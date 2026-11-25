# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Memory (SPEC §5.4): approved facts about each person. Reading is free; every change is a `send` (§5.2)."""

from __future__ import annotations

import secrets
import time

from ..accounts import Account
from ..approvals import Field
from . import guard
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

MAX_FACT_CHARS = 300
MAX_CONTEXT_CHARS = 7000  # about 2k tokens (SPEC §4.1, layer 3)
CARRY_ON = " Carry on with what you were doing; don't mention it unless the user asks."
NOT_AGAIN = "Don't propose it again in this conversation."


def render(items: list[dict], limit_chars: int | None = None) -> str:
    lines, total = [], 0
    for m in sorted(items, key=lambda m: m.get("updated", ""), reverse=True):
        line = f"- [{m['id']}] {m['text']}"
        total += len(line) + 1
        if limit_chars and total > limit_chars:
            lines.append(f"- … {len(items) - len(lines)} older memories not shown; use memory_search.")
            break
        lines.append(line)
    return "\n".join(lines)


def for_context(account: Account) -> str:
    items = account.load_memory()
    return render(items, MAX_CONTEXT_CHARS) if items else "(nothing saved yet)"


def add_fact(account: Account, text: str) -> dict:
    items = account.load_memory()
    now = time.strftime("%Y-%m-%d")
    item = {"id": secrets.token_hex(3), "text": text[:MAX_FACT_CHARS], "created": now, "updated": now}
    items.append(item)
    account.save_memory(items)
    return item


async def memory_list(args: dict, ctx: ToolContext) -> ToolResult:
    items = ctx.account.load_memory()
    return ToolResult(render(items) if items else "Nothing is remembered about the user yet.")


async def memory_search(args: dict, ctx: ToolContext) -> ToolResult:
    words = [w for w in str(args.get("query", "")).lower().split() if len(w) > 1]
    hits = [m for m in ctx.account.load_memory() if any(w in m["text"].lower() for w in words)]
    return ToolResult(render(hits) if hits else "No remembered facts match.")


async def memory_add(args: dict, ctx: ToolContext) -> ToolResult:
    text = " ".join(str(args.get("text", "")).split())[:MAX_FACT_CHARS]
    if not text:
        return ToolResult("Nothing to save: the fact is empty.", error=True)
    d, refused = await guard.send(ctx, "memory", "Remember this?", [Field("Fact", text)], then=NOT_AGAIN)
    if refused or not d.values[0].strip():
        return refused or guard.stopped(d, NOT_AGAIN)
    item = add_fact(ctx.account, " ".join(d.values[0].split()))
    return ToolResult(f"Saved [{item['id']}]: {item['text']}{CARRY_ON}")


async def memory_update(args: dict, ctx: ToolContext) -> ToolResult:
    mid, text = str(args.get("id", "")), " ".join(str(args.get("text", "")).split())[:MAX_FACT_CHARS]
    current = next((m for m in ctx.account.load_memory() if m["id"] == mid), None)
    if current is None:
        return ToolResult(f"No remembered fact has id {mid}. Use memory_list to see the ids.", error=True)
    title = f"Change “{current['text']}” to:"
    d, refused = await guard.send(ctx, "memory", title, [Field("Fact", text)], then=NOT_AGAIN)
    if refused or not d.values[0].strip():
        return refused or guard.stopped(d, NOT_AGAIN)
    items = ctx.account.load_memory()
    for m in items:
        if m["id"] == mid:
            m["text"], m["updated"] = " ".join(d.values[0].split())[:MAX_FACT_CHARS], time.strftime("%Y-%m-%d")
    ctx.account.save_memory(items)
    return ToolResult(f"Updated [{mid}]: {' '.join(d.values[0].split())[:MAX_FACT_CHARS]}{CARRY_ON}")


async def memory_delete(args: dict, ctx: ToolContext) -> ToolResult:
    mid = str(args.get("id", ""))
    current = next((m for m in ctx.account.load_memory() if m["id"] == mid), None)
    if current is None:
        return ToolResult(f"No remembered fact has id {mid}. Use memory_list to see the ids.", error=True)
    _, refused = await guard.send(
        ctx, "forget", "Forget this?", [Field("Fact", current["text"], "bool")], then=NOT_AGAIN
    )
    if refused:
        return refused
    ctx.account.save_memory([m for m in ctx.account.load_memory() if m["id"] != mid])
    return ToolResult(f"Forgot [{mid}].{CARRY_ON}")


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "memory_add",
            "Save one lasting fact about the user.",
            params(["text"], text=string("One short fact, e.g. 'Prefers to be called Fe'")),
            memory_add,
        )
    )
    reg.add(
        Tool(
            "memory_update",
            "Replace a remembered fact that changed.",
            params(["id", "text"], id=string("The fact's id, e.g. 3f9a1c"), text=string("The new version")),
            memory_update,
        )
    )
    reg.add(
        Tool(
            "memory_delete",
            "Forget a remembered fact that is wrong or no longer true.",
            params(["id"], id=string("The fact's id")),
            memory_delete,
        )
    )
    reg.add(Tool("memory_list", "List everything remembered about the user.", params(), memory_list))
    reg.add(
        Tool(
            "memory_search",
            "Search remembered facts about the user by keywords.",
            params(["query"], query=string("Keywords")),
            memory_search,
        )
    )
