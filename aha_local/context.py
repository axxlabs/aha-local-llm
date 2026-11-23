# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Building the model's context for one continuous chat (SPEC §4).

Layers, in this order:
  1. system prompt (static, byte-identical for every request and account) + tool definitions
  2-4. a <context> message: today's date, the person, their memories, the conversation summary
  4. recent turns: the person's messages and aha's final answers
  5. the current task: its tool calls and results

llama.cpp reuses its work for the longest prefix shared with an earlier request, so everything that changes
comes after everything that doesn't. The tool traffic of a task is dropped when the task ends; only the
final answer stays, and the full traffic remains in the transcript file.
"""

from __future__ import annotations

import datetime as dt
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .accounts import Account
from .tools import memory

PROMPTS = Path(__file__).parent / "prompts"
CHARS_PER_TOKEN = 3.2
IMAGE_TOKENS = 1200
TURNS_BUDGET = 20_000  # tokens of recent turns before the oldest are folded into the summary
SUMMARY_CHARS = 10_000  # about 3k tokens
TASK_SHARE = 0.6  # past this share of the context, older page snapshots in the task are shortened
KEEP_PAGES = 2  # the newest page results always stay whole

SUMMARY_PROMPT = """You keep a running summary of a long chat between a person and their assistant, aha.
Update the summary with the new turns below. Keep: commitments, bookings and their details (dates, times,
places, reference numbers), open questions, decisions, plans, and anything the person may refer back to.
Drop small talk and anything already done and irrelevant. Write short dated bullet points, newest last,
at most about 600 words. Reply with the updated summary only."""


@cache
def system_prompt() -> str:
    return "\n\n".join((PROMPTS / f).read_text().strip() for f in ("core.md", "browser.md", "chat.md"))


def estimate(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                total += (
                    IMAGE_TOKENS if part.get("type") == "image_url" else len(part.get("text", "")) / CHARS_PER_TOKEN
                )
        else:
            total += len(content or "") / CHARS_PER_TOKEN
        for call in m.get("tool_calls") or []:
            total += len(call["function"]["arguments"]) / CHARS_PER_TOKEN
    return int(total)


def context_block(account: Account, timezone: str, household: str, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(ZoneInfo(timezone))
    conv = account.load_context()
    summary = conv.get("summary") or "(no earlier conversation yet)"
    return (
        "<context>\n"
        f"Today: {now.strftime('%A %d %B %Y')} ({timezone}). Household: {household}.\n"
        f"You are talking with: {account.label} ({account.address.split(':', 1)[0]} chat).\n\n"
        "What you remember about them (each fact approved by them):\n"
        f"{memory.for_context(account)}\n\n"
        "Summary of your earlier conversation:\n"
        f"{summary}\n"
        "</context>"
    )


def build(
    account: Account,
    task: list[dict],
    timezone: str,
    household: str,
    context_tokens: int,
    now: dt.datetime | None = None,
) -> list[dict]:
    conv = account.load_context()
    head = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": context_block(account, timezone, household, now)},
        {"role": "assistant", "content": "Understood."},
    ]
    turns = [{"role": t["role"], "content": t["content"]} for t in conv.get("turns", [])]
    return head + turns + shorten_task(task, context_tokens * TASK_SHARE)


def shorten_task(task: list[dict], budget: float) -> list[dict]:
    """Within a long task, replace older page snapshots and images by one-line stubs (SPEC §4.3).

    Past the budget, every page but the newest KEEP_PAGES becomes a stub, not just enough of the oldest to fit:
    then each new step only changes the end of the task and the model server keeps the rest cached. Stubbing the
    oldest until it fitted changed the middle on every step, and each step read ~75k tokens again (seen: 50 s a
    step on evri.com)."""
    out = [dict(m) for m in task]
    if estimate(out) <= budget:
        return [strip(m) for m in out]
    pages = [i for i, m in enumerate(out) if m.get("_kind") in ("page", "image")]
    for i in pages[:-KEEP_PAGES]:
        m = out[i]
        if m.get("_kind") == "image":
            out[i] = {"role": "user", "content": "[an earlier screenshot, no longer shown]", "_kind": "stub"}
        else:
            first = (m.get("content") or "").splitlines()[:3]
            out[i] = {**m, "content": "[earlier page result, superseded] " + " ".join(first)[:200], "_kind": "stub"}
    return [strip(m) for m in out]


def strip(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


def record_turn(account: Account, user_text: str, answer: str, checked: bool = False) -> dict:
    """`checked`: the answer passed the number check, so its numbers may be quoted again later."""
    conv = account.load_context()
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    conv.setdefault("turns", []).extend(
        [
            {"role": "user", "content": user_text, "ts": stamp},
            {"role": "assistant", "content": answer, "ts": stamp, "checked": checked},
        ]
    )
    account.save_context(conv)
    return conv


def turns_to_fold(conv: dict) -> list[dict]:
    """The oldest turns to fold into the summary, so the rest fit in half the budget. Empty if within budget."""
    turns = conv.get("turns", [])
    if estimate(turns) <= TURNS_BUDGET:
        return []
    fold = []
    while turns[len(fold) :] and estimate(turns[len(fold) :]) > TURNS_BUDGET / 2:
        fold.extend(turns[len(fold) : len(fold) + 2])
    return fold


def apply_fold(account: Account, folded: list[dict], summary: str) -> None:
    conv = account.load_context()
    turns = conv.get("turns", [])
    if turns[: len(folded)] != folded:
        return  # the history was cleared or cut while the summary was written (SPEC §3.8.1)
    # The summary is as old as its oldest turn; one from before this was recorded stays undated, and a history
    # length drops it (SPEC §3.8.1).
    if not conv.get("summary") and folded and folded[0].get("ts"):
        conv["summary_from"] = folded[0]["ts"]
    conv["turns"] = turns[len(folded) :]
    conv["summary"] = summary.strip()[:SUMMARY_CHARS]
    account.save_context(conv)


def fold_request(conv: dict, folded: list[dict]) -> list[dict]:
    lines = "\n".join(
        f"[{t.get('ts', '')}] {'Person' if t['role'] == 'user' else 'aha'}: {t['content']}" for t in folded
    )
    return [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": f"Current summary:\n{conv.get('summary') or '(empty)'}\n\nNew turns:\n{lines}"},
    ]


def forget_last(account: Account) -> bool:
    conv = account.load_context()
    if len(conv.get("turns", [])) < 2:
        return False
    conv["turns"] = conv["turns"][:-2]
    account.save_context(conv)
    return True
