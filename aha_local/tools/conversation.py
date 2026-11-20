# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""conversation_search over the account's own full transcript (SPEC §4.4) and current_time."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .registry import Registry, Tool, ToolContext, ToolResult, params, string

MAX_HITS = 8


async def conversation_search(args: dict, ctx: ToolContext) -> ToolResult:
    words = [w for w in str(args.get("query", "")).lower().split() if len(w) > 2]
    if not words:
        return ToolResult("Give some keywords to search for.", error=True)
    hits = []
    for e in reversed(ctx.account.read_transcript()):
        if e.get("kind") not in ("user", "answer"):
            continue
        text = e.get("text", "")
        score = sum(w in text.lower() for w in words)
        if score:
            hits.append((score, e))
        if len(hits) >= 200:
            break
    if not hits:
        return ToolResult("Nothing in our earlier conversation matches.")
    hits.sort(key=lambda x: -x[0])  # stable: among equal scores, newest stays first
    lines = [
        f"{e.get('ts', '')[:16]} {'User' if e['kind'] == 'user' else 'aha'}: {e['text'][:500]}"
        for _, e in hits[:MAX_HITS]
    ]
    return ToolResult("From our earlier conversation (newest matches first):\n" + "\n".join(lines))


async def message_user(args: dict, ctx: ToolContext) -> ToolResult:
    text = str(args.get("text", "")).strip()
    if not text:
        return ToolResult("Give the text to send.", error=True)
    if ctx.send is None:
        return ToolResult("Can't send messages right now.", error=True)
    await ctx.send(text[:1000])
    return ToolResult("Sent. Carry on with the task.")


async def current_time(args: dict, ctx: ToolContext) -> ToolResult:
    now = dt.datetime.now(ZoneInfo(ctx.config.timezone))
    return ToolResult(now.strftime(f"%A %d %B %Y, %H:%M ({ctx.config.timezone}, UTC%z)"))


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "conversation_search",
            "Search everything said earlier in this conversation with the user, "
            "including older messages that are no longer in view.",
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
