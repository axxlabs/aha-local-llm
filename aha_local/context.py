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

from . import dates, topics
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
def system_prompt(model: str = "") -> str:
    """The base prompt with the skills' list, then the model's own file when it has one (SPEC §3.5): the same for
    every request and account on that model. Another model's prompt cache starts empty anyway."""
    from . import skills

    parts = [
        (PROMPTS / "core.md").read_text().strip().replace("<skills/>", skills.listing()),
        (PROMPTS / "chat.md").read_text().strip(),
    ]
    own = PROMPTS / "models" / f"{model}.md"
    if model and own.is_file():
        parts.append(own.read_text().strip())
    return "\n\n".join(parts)


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
        f"{active_topics(account)}"
        "Other topics of this conversation aren't shown here: find them with conversation_search, or read one by "
        "its number with topic_read.\n"
        "</context>"
    )


def active_topics(account: Account) -> str:
    """The topics that go on without the person in them, so the model knows they exist (SPEC §4.1 "Context")."""
    lines = topics.context_lines(account)
    return ("Topics going on in the background:\n" + "\n".join(lines) + "\n\n") if lines else ""


CUT = "[{count} earlier messages of this topic aren't shown here; topic_read {topic} reads them]\n\n"


def topic_turns(turns: list[dict], topic: int) -> list[dict]:
    """The turns of topic `topic` within the budget: its first exchange always (the goal and its limits, SPEC §4.1),
    then the newest that fit. Where turns were left out, the first one after the gap says so, with how to read them
    (the harness's words, only when something was cut)."""
    mine = [t for t in turns if t.get("topic") == topic]
    if estimate(mine) <= TURNS_BUDGET:
        return mine
    first, rest = mine[:2], mine[2:]
    cut = 0
    while rest and estimate(first + rest) > TURNS_BUDGET:
        rest, cut = rest[2:], cut + 2
    if not rest:
        return first
    head = {**rest[0], "content": CUT.format(count=cut, topic=topic) + rest[0]["content"]}
    return [*first, head, *rest[1:]]


def build(
    account: Account,
    task: list[dict],
    timezone: str,
    household: str,
    context_tokens: int,
    now: dt.datetime | None = None,
    topic: int = 0,
    model: str = "",
) -> list[dict]:
    """`topic`: the topic the run works in: only its turns are shown (SPEC §4.1)."""
    turns = topic_turns(account.load_context()["turns"], topic)
    head = [
        {"role": "system", "content": system_prompt(model)},
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
    return "\n\n[sha's record of what this request did:\n" + "\n".join(f"- {d}" for d in record) + "]"


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
            stub = "[earlier page result, superseded] " + " ".join(first)[:200]
            if m.get("_skill"):  # the skill added with it stays (SPEC §3.5 "Skills")
                stub += "\n\n" + m["_skill"]
            out[i] = {**m, "content": stub, "_kind": "stub"}
    return [strip(m) for m in out]


def strip(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


KEEP_TURNS = 30 * 86400  # a quiet topic's turns are kept this long; an active one's (scheduled, waiting) always


def record_turn(account: Account, topic: int, user_text: str, answer: str, record: list[str] | None = None) -> dict:
    """One exchange in topic `topic`. `record`: what the request changed (SPEC §4 "What a request did"), shown with
    the answer to later requests in the topic."""
    conv = account.load_context()
    now = time.time()
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    conv["turns"].extend(
        [
            {"role": "user", "content": user_text, "ts": stamp, "t": now, "topic": topic},
            {
                "role": "assistant",
                "content": answer,
                "ts": stamp,
                "t": now,
                "topic": topic,
                **({"record": record[-MAX_RECORD:]} if record else {}),
            },
        ]
    )
    live = {e["n"] for e in topics.active(account)} | {topic}
    conv["turns"] = [t for t in conv["turns"] if t.get("topic") in live or turn_time(t) >= now - KEEP_TURNS]
    account.save_context(conv)
    return conv


def save_topic(account: Account, n: int, title: str, text: str, started: float = 0) -> None:
    """Topic n's memory entry, replacing its earlier one (SPEC §5.4)."""
    conv = account.load_context()
    entries = [e for e in conv["topics"] if e.get("n") != n]
    text = text.strip()[:TOPIC_CHARS]
    if text:
        entries.append({"id": f"t{n}", "n": n, "title": title, "text": text, "ts": started or time.time()})
    conv["topics"] = entries
    account.save_context(conv)


def forget_topic(account: Account, topic_id: str) -> bool:
    conv = account.load_context()
    kept = [e for e in conv["topics"] if e.get("id") != topic_id]
    if len(kept) == len(conv["topics"]):
        return False
    conv["topics"] = kept
    account.save_context(conv)
    return True
