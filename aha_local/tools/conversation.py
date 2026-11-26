# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""conversation_search over the account's own full transcript (SPEC §4.4) and current_time."""

from __future__ import annotations

import datetime as dt

from .. import dates
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

MAX_HITS = 8
MAX_TOPICS = 5


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
            out.append(f"- [{e['id']}] {day}, {e.get('title') or 'Untitled'}:\n  " + e["text"].replace("\n", "\n  "))
    if hits:
        out.append("What was said (newest matches first):")
        out += [
            f"{e.get('ts', '')[:16]} {'User' if e['kind'] == 'user' else 'aha'}: {e['text'][:500]}"
            for _, e in hits[:MAX_HITS]
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
            "message_user",
            "Send the user a short message now and keep working: a progress update when they ask how it's going, "
            "or a quick reply to something they said while you were working. Your final answer still ends the task.",
            params(["text"], text=string("The message, one or two sentences")),
            message_user,
        )
    )
    reg.add(Tool("current_time", "The current date and time in the household's timezone.", params(), current_time))
