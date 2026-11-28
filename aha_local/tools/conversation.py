# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""conversation_search over the account's own full transcript (SPEC §4.4) and current_time."""

from __future__ import annotations

import datetime as dt

from .. import dates, topics
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

MAX_HITS = 8
MAX_TOPICS = 5
MAX_READ = 12_000  # characters of a topic read back: its newest messages when it's longer


async def conversation_search(args: dict, ctx: ToolContext) -> ToolResult:
    """The memory entries of earlier topics (what was found and done, with its details), then what was said
    (SPEC §4.4): the topics aren't in every request's context, so this is how the model finds them."""
    words = [w for w in str(args.get("query", "")).lower().split() if len(w) > 2]
    if not words:
        return ToolResult("Give some keywords to search for.", error=True)
    score = lambda text: sum(w in text.lower() for w in words)  # noqa: E731
    topics = sorted(ctx.account.load_context()["topics"], key=lambda e: -e.get("ts", 0))
    found = [e for e in topics if score(f"{e.get('title', '')} {e['text']}")]
    found.sort(key=lambda e: -score(f"{e.get('title', '')} {e['text']}"))  # stable: newest first among equals
    hits = []
    for e in reversed(ctx.account.read_transcript()):
        if e.get("kind") not in ("user", "answer"):
            continue
        if n := score(e.get("text", "")):
            hits.append((n, e))
        if len(hits) >= 200:
            break
    if not found and not hits:
        return ToolResult("Nothing in our earlier conversation matches.")
    hits.sort(key=lambda x: -x[0])
    out = []
    if found:
        out.append("Your memory of earlier topics (best matches first):")
        for e in found[:MAX_TOPICS]:
            day = dates.say(dt.date.fromtimestamp(e["ts"])) if e.get("ts") else "earlier"
            label = f"topic {e['n']}"
            out.append(f"- [{label}] {day}, {e.get('title') or 'Untitled'}:\n  " + e["text"].replace("\n", "\n  "))
    if hits:
        out.append("What was said (newest matches first):")
        out += [
            f"{e.get('ts', '')[:16]} {'User' if e['kind'] == 'user' else 'sha'}: {e['text'][:500]}"
            for _, e in hits[:MAX_HITS]
        ]
    return ToolResult("\n".join(out))


async def topic_read(args: dict, ctx: ToolContext) -> ToolResult:
    """One topic, read back (SPEC §4.1, §7.4 "Topic numbers"): its messages, your memory of it, and whether it
    checks on its own. Read only: the topic you're working in stays the one in view."""
    try:
        n = int(str(args.get("number", "")).strip().lstrip("#"))
    except ValueError:
        return ToolResult("Give the topic's number, e.g. 12.", error=True)
    account = ctx.account
    entry = topics.get(account, n)
    if entry is None:
        return ToolResult(f"There's no topic {n}.", error=True)
    tz = dates.tz(ctx.config) if ctx.config else None
    lines = []
    for it in account.items(after=max(0, entry.get("first", 1) - 1)):
        if it.get("t") != "msg" or it.get("topic") != n:
            continue
        when = dt.datetime.fromtimestamp(it.get("ts", 0), tz).strftime("%d/%m/%Y %H:%M")
        who = "User" if it.get("role") == "me" else "sha"
        lines.append(f"{when} {who}: {it.get('text', '')}")
    if not lines:
        return ToolResult(f"Topic {n} has no messages left (the history before it was cleared).", error=True)
    body = "\n".join(lines)
    if len(body) > MAX_READ:
        body = "(earlier messages cut)\n" + body[-MAX_READ:]
    head = f"Topic {n}" + (f": {entry['title']}" if entry.get("title") else "")
    memory = next((e["text"] for e in account.load_context()["topics"] if e.get("n") == n), "")
    out = [head, "", body]
    if memory:
        out += ["", "Your memory of it:", memory]
    if s := entry.get("schedule"):
        from . import background

        out += [
            "",
            f"It checks on its own ({background.schedule_words(s)}). Waiting for: {s['title']}. "
            f"How: {s['prompt']} Done when: {s['done_when']}.",
        ]
    return ToolResult("\n".join(out))


async def message_user(args: dict, ctx: ToolContext) -> ToolResult:
    text = str(args.get("text", "")).strip()
    if not text:
        return ToolResult("Give the text to send.", error=True)
    if ctx.send is None:
        return ToolResult("Can't send messages right now.", error=True)
    await ctx.send(text[:1000])
    return ToolResult("Sent. Carry on with the task.")


async def current_time(args: dict, ctx: ToolContext) -> ToolResult:
    now = dates.now(ctx)
    return ToolResult(now.strftime(f"%d/%m/%Y %H:%M, a %A ({ctx.config.timezone}, UTC%z)"))


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "conversation_search",
            "Search earlier topics of this conversation with the user: your memory of each (what was asked, "
            "found and done, with codes, addresses and dates) and the messages themselves.",
            params(["query"], query=string("Keywords")),
            conversation_search,
        )
    )
    reg.add(
        Tool(
            "topic_read",
            'Read an earlier topic by its number, as the user sees it on the chat\'s folded lines ("topic 12"): its '
            "messages, your memory of it, and whether it checks on its own. Read only: you stay in the topic you're "
            "working in.",
            params(["number"], number={"type": "integer", "description": "The topic's number"}),
            topic_read,
        )
    )
    reg.add(
        Tool(
            "message_user",
            "Send the user a short message now and keep working: a progress update when they ask how it's going, "
            "or a quick reply to something they said while you were working. Your final answer still ends the task.",
            params(["text"], text=string("The message, one or two sentences")),
            message_user,
        )
    )
    reg.add(Tool("current_time", "The current date and time in the household's timezone.", params(), current_time))
