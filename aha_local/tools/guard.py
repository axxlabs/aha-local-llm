# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Running actions through the gate (SPEC §5.1, §5.2).

The tools describe an action; this module asks the gate with what the task knows, writes trusted runs to the
audit, shows the card when the gate says so, and words the outcome the same way whatever happened: the model
never learns whether a card was shown, approved as it was, edited, or skipped because of trust.

This is the only module that shows cards (`ask`): every tool reaches the person through it, so every card is
audited and declined the same way (tests check no tool calls `approvals.ask` itself).

"Always allow for this task" (SPEC §5.3): every action card has the tick. Ticked and approved, the same action
(searching, typing on that site, an email to those people…, its `allow_key`) runs without a card for the rest of
the request; for a background task, for all its checks, kept on the task and listed in the Tasks panel.
"""

from __future__ import annotations

from ..approvals import Decision, Field
from ..items import Image
from ..policy import addresses
from ..policy.browser import open_action
from ..policy.chats import user_driven
from ..policy.gate import Action, Facts, Verdict, decide, explain
from ..policy.urls import check_url
from . import trusted
from .registry import ToolContext, ToolResult

STOPPED = "This wasn't done: the user stopped it."
STOPPED_FOR_MESSAGE = "This wasn't done: the user didn't allow it."
MESSAGE_FOLLOWS = "They sent a new message instead: read it before you go on."


def facts(ctx: ToolContext) -> Facts:
    return Facts(
        trusted=set(trusted.sites(ctx.account)) if ctx.account is not None else set(),
        shown=ctx.task.seen_urls,
        opened=ctx.task.opened_sites,
    )


def allow_key(action: Action) -> str:
    """What an "always" tick on this action's card covers: the same kind of action, to the same place or people."""
    if action.kind == "send" and action.site:  # a chat message: "always" only for the chat the page named
        return f"message:{action.site}:{action.chat}" if action.chat else ""
    if user_driven(action.kind, action.site):  # typing on a chat site (its search box): never "always"
        return ""
    if action.kind == "send" and action.what == "task":  # each background task is its own decision (2026-11-27)
        return ""
    if action.kind == "send":
        return f"{action.what}:{','.join(sorted(action.to))}" if action.to else action.what
    if action.kind == "search":
        return "search"
    return f"{action.kind}:{action.site}" if action.site else action.kind


ALLOW_WORDS = {
    "search": "Search Google",
    "type": "Type and fill in forms on {x}",
    "open": "Open {x}",
    "message": "Send messages on {x}",
    "email": "Send email to {x}",
    "draft": "Save email drafts to {x}",
    "calendar": "Add calendar events (guests: {x})",
    "calendar_change": "Change calendar events (guests: {x})",
    "calendar_delete": "Delete calendar events",
    "memory": "Save memories",
    "memory_update": "Change memories",
    "forget": "Forget memories",
    "task": "Start background tasks",
    "other": "Run other browser actions on {x}",
}


def allow_label(key: str) -> str:
    """The person's words for an "always" (the Tasks panel's approved permissions)."""
    kind, _, x = key.partition(":")
    if kind == "message" and ":" in x:
        site, _, chat = x.partition(":")
        return f"Send messages to {chat} on {site}"
    words = ALLOW_WORDS.get(kind, kind.replace("_", " ").capitalize())
    return words.replace("{x}", x.replace(",", ", ")) if x else words.replace(" to {x}", "").replace(" on {x}", "")


def check(ctx: ToolContext, action: Action, tool: str) -> Verdict:
    """The gate's verdict, then the task's "always": a card the person ticked runs from then on. A run because of
    trust or an "always" is audited (the tool and what it covers, never the values)."""
    verdict = decide(action, facts(ctx))
    if verdict.outcome == "card" and (key := allow_key(action)) and key in ctx.task.allowed:
        verdict = Verdict("run", "allowed")
    if verdict.why in ("trusted", "allowed") and ctx.account is not None:
        covered = allow_key(action) if verdict.why == "allowed" else action.site or action.what or action.kind
        ctx.account.audit({verdict.why: covered, "tool": tool})
    return verdict


def task_box(ctx: ToolContext) -> dict | None:
    """The background task a card is raised in, for the app to draw it in that task's box (SPEC §7.4)."""
    if not ctx.task.background or ctx.account is None:
        return None
    from . import background

    entry = next((e for e in background.load(ctx.account) if e.get("id") == ctx.task.background), None)
    return {"id": ctx.task.background, "title": entry["title"] if entry else ""}


def remember(ctx: ToolContext, key: str) -> None:
    """The person ticked "always" on a card: this request, or this background task's every check, allows it."""
    ctx.task.allowed.add(key)
    if ctx.task.background and ctx.account is not None:
        from . import background  # the task's own list (the Tasks panel shows it)

        background.allow(ctx.account, ctx.task.background, key)


def blocked(why: str) -> ToolResult:
    """An action the policy blocks: the same words wherever it's blocked."""
    return ToolResult(f"Blocked by SHA's safety policy: {why}.", error=True)


def stopped(decision: Decision, then: str = "", declined: str = "") -> ToolResult:
    """A card the person said no to (or stopped): the same words for every kind of card. `declined`: what a
    card that isn't an action (a check the person does) says instead."""
    if decision.reason == "message":  # they wrote instead of answering: their message comes next
        return ToolResult(f"{declined or STOPPED_FOR_MESSAGE} {MESSAGE_FOLLOWS}", error=True)
    if declined:
        text = declined + (f" They said: “{decision.note}”. Follow that." if decision.note else "")
        return ToolResult(text, error=True)
    text = STOPPED
    if decision.note:
        text += f" They said: “{decision.note}”. Follow that."
    else:
        text += " " + (then or "Don't retry it unchanged; ask them how they'd like to go on.")
    return ToolResult(text, error=True)


async def ask(
    ctx: ToolContext,
    kind: str,
    title: str,
    fields: list[Field],
    image: Image | None = None,
    then: str = "",
    why: str = "",
    declined: str = "",
    allow: str = "",
) -> tuple[Decision, ToolResult | None]:
    """Show a card. (decision, None) when approved; (decision, the result to return) when not. `why`: the gate's
    reason, for the card's (i) (policy.gate.explain); without it, the kind's own (approvals.KINDS). `allow`: the
    action's `allow_key`, for the card's "Always allow for this task" tick (none on cards the person fills in:
    a sign-in, a human check)."""
    decision = await ctx.approvals.ask(
        ctx.account, kind, title, fields, image, why=why, always=bool(allow), task=task_box(ctx)
    )
    if decision.approved and decision.always and allow:
        remember(ctx, allow)
    return decision, (None if decision.approved else stopped(decision, then, declined))


async def send(
    ctx: ToolContext,
    kind: str,
    title: str,
    fields: list[Field],
    then: str = "",
    to: list[str] | None = None,
    what: str = "",
) -> tuple[Decision, ToolResult | None]:
    """Something that goes out in the person's name or changes what they keep (an email, an event, a memory, a
    background task): a `send` for the gate (SPEC §5.2 rule 2), always a card, trusted or not, unless the person
    ticked "always" on one for the same kind and people in this task (`to`: the addresses; `what`: the kind
    when not the card's, as changing an event). Then it runs as it is."""
    action = Action("send", what=what or kind, to=None if to is None else tuple(to))
    verdict = check(ctx, action, kind)
    if verdict.outcome == "block":
        return Decision(False, reason="blocked"), blocked(verdict.why)
    if verdict.outcome == "run":
        return Decision(True, [f.value for f in fields]), None
    return await ask(ctx, kind, title, fields, then=then, allow=allow_key(action))


async def open_address(ctx: ToolContext, url: str, tool: str) -> tuple[str, Action, ToolResult | None]:
    """Opening an address, in the browser or by fetch_url, through the gate (SPEC §5.2 rule 7): its action, and the
    result to return instead when it doesn't open."""
    action = open_action(url)
    verdict = check(ctx, action, tool)
    if verdict.outcome == "block":
        return url, action, blocked(verdict.why)
    if verdict.outcome == "refuse":
        return url, action, ToolResult(SEARCH_INSTEAD if verdict.why == "search" else made_up(url), error=True)
    if verdict.outcome == "card":
        fields = [Field("Address", url, "info")]
        why = explain(verdict.why, action.site)
        _, refused = await ask(ctx, "action", f"Open {action.site}?", fields, why=why, allow=allow_key(action))
        if refused:
            return url, action, refused
    return url, action, None


SEARCH_INSTEAD = "Not opened: that's a search engine's results page. Search with web_search instead."


def made_up(url: str) -> str:
    home = addresses.home(url)
    return (
        f"Not opened: {url} isn't in anything you read in this task or the user sent, so you put it together "
        f"yourself, and such addresses are usually wrong. Open the site's home page, {home}, and find the page from "
        "there (its menus, links or search box), or search the web for it. Never write another address yourself."
    )


def opened(ctx: ToolContext, url: str) -> None:
    """An address that was opened (it ran, or the person approved it): its site counts as opened in the task and
    the address as shown, so neither asks again (SPEC §5.2)."""
    site, _ = check_url(url)
    if site:
        ctx.task.opened_sites.add(site)
    if k := addresses.key(url):
        ctx.task.seen_urls.add(k)
