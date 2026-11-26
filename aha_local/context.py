# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Building the model's context for one continuous chat (SPEC §4).

Layers, in this order:
  1. system prompt (static, byte-identical for every request and account) + tool definitions
  2. a <context> message: today's date, the person and their profile (approved facts)
  3. the turns of the topic going on now: the person's messages and aha's final answers
  4. the current task: its tool calls and results

Older chat isn't in the context: each topic leaves its memory entry (SPEC §5.4), which the model finds with
conversation_search when it needs it, with the rest of the history (email, calendar too). The approved facts
are always in view.

llama.cpp reuses its work for the longest prefix shared with an earlier request, so everything that changes
comes after everything that doesn't. The tool traffic of a task is dropped when the task ends; only the
final answer stays, and the full traffic remains in the transcript file.
"""

from __future__ import annotations

import datetime as dt
import time
from functools import cache
from pathlib import Path

from . import dates
from .accounts import Account, turn_time
from .tools import memory

PROMPTS = Path(__file__).parent / "prompts"
CHARS_PER_TOKEN = 3.2
IMAGE_TOKENS = 1200
TURNS_BUDGET = 20_000  # tokens of the topic's turns; a longer topic keeps its newest
TOPIC_CHARS = 1_200  # one topic's entry
TASK_SHARE = 0.6  # past this share of the context, older page snapshots in the task are shortened
KEEP_PAGES = 2  # the newest page results always stay whole


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


def days_ahead(now: dt.datetime, count: int = 14) -> str:
    """The coming days with their weekdays, so "this Saturday" needs no date arithmetic (seen: the 29th taken for a
    Saturday; it was a Sunday). Changes once a day, like the date above it."""
    return ", ".join((now + dt.timedelta(days=i)).strftime("%a %d/%m/%Y") for i in range(1, count + 1))


def context_block(account: Account, timezone: str, household: str, now: dt.datetime | None = None) -> str:
    now = now or dates.now(timezone)
    return (
        # The date comes before the memory, so the model reads every fact knowing what day it is. The cost: a new
        # day changes the block from the date on, so the memory is read again once a day (SPEC §4).
        "<context>\n"
        f"You are talking with: {account.label} ({account.address.split(':', 1)[0]} chat). Household: {household}.\n"
        f"Today: {now.strftime('%d/%m/%Y')}, a {now.strftime('%A')} ({timezone}).\n"
        f"The next 14 days: {days_ahead(now)}.\n\n"
        "What you remember about them (each fact approved by them):\n"
        f"{memory.for_context(account)}\n\n"
        "Earlier topics of this conversation aren't shown here: find them with conversation_search.\n"
        "</context>"
    )


def topic_turns(turns: list[dict], since: float) -> list[dict]:
    """The turns of the topic going on: recorded at or after `since`, the newest that fit the budget."""
    kept = [t for t in turns if turn_time(t) >= since]
    while kept and estimate(kept) > TURNS_BUDGET:
        kept = kept[2:]
    return kept


def build(
    account: Account,
    task: list[dict],
    timezone: str,
    household: str,
    context_tokens: int,
    now: dt.datetime | None = None,
    topic: tuple[int, float] | None = None,
) -> list[dict]:
    """`topic`: (first chat item, when) of the topic going on (folding.segment_start): only its turns are shown."""
    turns = topic_turns(account.load_context()["turns"], topic[1] if topic else 0)
    head = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": context_block(account, timezone, household, now)},
        {"role": "assistant", "content": "Understood."},
    ]
    shown = [{"role": t["role"], "content": t["content"] + did_note(t.get("record"))} for t in turns]
    return head + shown + shorten_task(task, context_tokens * TASK_SHARE)


MAX_RECORD = 40  # lines of a request's record kept: the latest


def did_note(record: object) -> str:
    """What an earlier request changed, after its answer (SPEC §4 "What a request did"). A later request sees only
    the messages, not the tool calls: without this, a fact saved in a request that then ran out of steps looked
    still to do, and was asked twice more (2026-11-26)."""
    if not isinstance(record, list) or not record:
        return ""
    return "\n\n[Aha!'s record of what this request did:\n" + "\n".join(f"- {d}" for d in record) + "]"


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


def record_turn(account: Account, user_text: str, answer: str, record: list[str] | None = None) -> dict:
    """`record`: what the request changed (SPEC §4 "What a request did"), shown with the answer to later requests."""
    conv = account.load_context()
    now = time.time()
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    conv["turns"].extend(
        [
            {"role": "user", "content": user_text, "ts": stamp, "t": now},
            {
                "role": "assistant",
                "content": answer,
                "ts": stamp,
                "t": now,
                **({"record": record[-MAX_RECORD:]} if record else {}),
            },
        ]
    )
    account.save_context(conv)
    return conv


def save_topic(account: Account, start: int, started: float, title: str, text: str) -> None:
    """The topic starting at chat item `start` has this memory entry now, replacing its earlier one (SPEC §5.4);
    the turns of earlier topics leave the context."""
    conv = account.load_context()
    topics = [e for e in conv["topics"] if e.get("from") != start]
    text = text.strip()[:TOPIC_CHARS]
    if text:
        topics.append({"id": f"t{start}", "from": start, "title": title, "text": text, "ts": started})
    conv["topics"] = topics
    conv["turns"] = [t for t in conv["turns"] if turn_time(t) >= started]
    account.save_context(conv)


def forget_topic(account: Account, topic_id: str) -> bool:
    conv = account.load_context()
    kept = [e for e in conv["topics"] if e.get("id") != topic_id]
    if len(kept) == len(conv["topics"]):
        return False
    conv["topics"] = kept
    account.save_context(conv)
    return True
