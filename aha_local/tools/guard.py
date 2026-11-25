# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Running actions through the gate (SPEC §5.1, §5.2).

The tools describe an action; this module asks the gate with what the task knows, writes trusted runs to the
audit, shows the card when the gate says so, and words the outcome the same way whatever happened: the model
never learns whether a card was shown, approved as it was, edited, or skipped because of trust.

This is the only module that shows cards (`ask`): every tool reaches the person through it, so every card is
audited and declined the same way (tests check no tool calls `approvals.ask` itself).
"""

from __future__ import annotations

from ..approvals import Decision, Field
from ..items import Image
from ..policy import addresses
from ..policy.browser import open_action, open_form
from ..policy.gate import Action, Facts, Verdict, decide, explain
from ..policy.urls import check_url
from . import trusted
from .registry import ToolContext, ToolResult

STOPPED = "This wasn't done: the user stopped it."


def facts(ctx: ToolContext) -> Facts:
    return Facts(
        trusted=set(trusted.sites(ctx.account)) if ctx.account is not None else set(),
        private=ctx.task.private,
        shown=ctx.task.seen_urls,
        opened=ctx.task.opened_sites,
        allow_all_sites=ctx.config.allow_all_sites,
    )


def check(ctx: ToolContext, action: Action, tool: str) -> Verdict:
    """The gate's verdict; a run because of trust is audited (the tool and the site, never the values)."""
    verdict = decide(action, facts(ctx))
    if verdict.why == "trusted" and ctx.account is not None:
        ctx.account.audit({"trusted": action.site, "tool": tool, **({"private": True} if ctx.task.private else {})})
    return verdict


def blocked(why: str) -> ToolResult:
    """An action the policy blocks: the same words wherever it's blocked."""
    return ToolResult(f"Blocked by aha's safety policy: {why}.", error=True)


def stopped(decision: Decision, then: str = "", declined: str = "") -> ToolResult:
    """A card the person said no to (or stopped): the same words for every kind of card. `declined`: what a
    card that isn't an action (a check the person does) says instead."""
    if decision.reason == "expired":
        return ToolResult("", error=True, kind="expired")
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
) -> tuple[Decision, ToolResult | None]:
    """Show a card. (decision, None) when approved; (decision, the result to return) when not. `why`: the gate's
    reason, for the card's (i) (policy.gate.explain); without it, the kind's own (approvals.KIND_WHY)."""
    decision = await ctx.approvals.ask(ctx.account, kind, title, fields, image, why=why)
    return decision, (None if decision.approved else stopped(decision, then, declined))


async def send(
    ctx: ToolContext, kind: str, title: str, fields: list[Field], then: str = ""
) -> tuple[Decision, ToolResult | None]:
    """Something that goes out in the person's name or changes what they keep (an email, an event, a memory, a
    scheduled task): a `send` for the gate (SPEC §5.2 rule 1), which shows it on a card, trusted or not."""
    verdict = check(ctx, Action("send"), kind)
    if verdict.outcome == "block":
        return Decision(False, reason="blocked"), blocked(verdict.why)
    return await ask(ctx, kind, title, fields, then=then)


async def open_address(ctx: ToolContext, url: str, tool: str) -> tuple[str, Action, ToolResult | None]:
    """Opening an address, in the browser or by fetch_url, through the gate (SPEC §5.2 rules 5–7): the address to
    open (as the person may have changed it on the card) and its action, or the result to return instead."""
    action = open_action(url)
    verdict = check(ctx, action, tool)
    if verdict.outcome == "block":
        return url, action, blocked(verdict.why)
    if verdict.outcome == "card":
        made_up = addresses.made_up(url, ctx.task.seen_urls)
        form = open_form(url, data=verdict.why.startswith("data"), made_up=made_up)
        decision, refused = await ask(ctx, "action", form.title, form.fields, why=explain(verdict.why, action.site))
        if refused:
            return url, action, refused
        url = decision.values[0].strip()
        action = open_action(url)  # what the person approved, checked again
        if action.kind == "block":
            return url, action, blocked(action.reason)
    return url, action, None


def opened(ctx: ToolContext, url: str) -> None:
    """An address that was opened (it ran, or the person approved it): its site counts as opened in the task and
    the address as shown, so neither asks again (SPEC §5.2)."""
    site, _ = check_url(url)
    if site:
        ctx.task.opened_sites.add(site)
    if k := addresses.key(url):
        ctx.task.seen_urls.add(k)
