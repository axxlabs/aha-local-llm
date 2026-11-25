# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Permissions up front (SPEC §3.4 "Permissions up front", §5.2 "Allowed up front").

The first step of every task: the model says what the request will need (sites to work on, searching after
reading private things, people to email, calendar changes), and the person allows it on one card, all at once,
instead of a card for each action as it happens. What they tick goes into the task's `Grant`, in code; the
agent keeps it for the topic, and a background task started from it keeps it for its checks. Anything not
allowed asks as usual when it happens.
"""

from __future__ import annotations

from ..approvals import Field
from ..policy.grants import CALENDAR_LABEL, SEARCH_LABEL, Grant, email_label, emails_in, site_label
from . import guard, trusted
from .registry import Registry, Tool, ToolContext, ToolResult

NAME = "permissions_request"
MAX_ITEMS = 20  # sites, and people, each

FIRST = (
    f"Not run: the first step of every task is {NAME}, with everything this request will need allowed (empty "
    "when it needs nothing). Call it now, then this again."
)
NOTHING = "Nothing needs allowing up front. Go ahead."
DECLINED = "Not allowed up front: each of these is asked of the user when it happens. Go ahead."
REPLAN = (
    "Nothing was allowed and nothing should be done yet: the user wants to change the plan. Don't call any other "
    "tool. Ask them in one short line what they'd like to change, and stop; when they answer, write the new "
    'numbered plan and ask "Shall I go ahead?" again.'
)

DESCRIPTION = (
    "The first step of every task, before any other tool (or in the same reply as your first other tool calls): "
    "everything this request will need from the user, allowed once, up front, instead of one by one as it "
    "happens. It also holds for a background task you start from this request. Think through the steps, then "
    "ask for exactly what they need, no more: the sites you'll fill in forms, book, buy or sign in on (sites); "
    "whether you'll search the web after reading their email or a signed-in page (search); the people you'll "
    "email or invite, by address (email); whether you'll add or change their calendar (calendar). Leave all "
    "empty when the request needs none of these (a question, a search, reading their email). Anything you "
    "didn't ask for is asked when it happens. Call it again later only if the plan changed."
)


def _sites(values: object) -> tuple[list[str], list[str]]:
    """(sites, what wasn't a site)."""
    good, bad = [], []
    for v in values if isinstance(values, list) else []:
        site = trusted.valid(str(v))
        (good if site else bad).append(site or str(v))
    return list(dict.fromkeys(good)), bad


def _people(values: object) -> tuple[list[str], list[str]]:
    good, bad = [], []
    for v in values if isinstance(values, list) else []:
        found = emails_in(str(v))
        (good if len(found) == 1 else bad).append(found[0] if len(found) == 1 else str(v))
    return list(dict.fromkeys(good)), bad


async def permissions_request(args: dict, ctx: ToolContext) -> ToolResult:
    sites, bad_sites = _sites(args.get("sites"))
    people, bad_people = _people(args.get("email"))
    if bad_sites or bad_people:
        wrong = ", ".join([*bad_sites, *bad_people])
        return ToolResult(
            f"Not asked: {wrong} isn't a site's name (like thefork.co.uk) or one email address. Fix it and call "
            f"{NAME} again.",
            error=True,
        )
    if len(sites) > MAX_ITEMS or len(people) > MAX_ITEMS:
        return ToolResult(f"Not asked: at most {MAX_ITEMS} sites and {MAX_ITEMS} people. Ask for fewer.", error=True)
    ctx.task.estimated = True
    have, free = ctx.task.grant, set(trusted.sites(ctx.account)) if ctx.account is not None else set()
    # What isn't allowed yet: trusted sites never ask, and what the topic already allowed stays allowed.
    asked = Grant(
        {s for s in sites if s not in free and s not in have.sites},
        args.get("search") is True and not have.search,
        {p for p in people if p not in have.email},
        args.get("calendar") is True and not have.calendar,
    )
    if not asked:
        return ToolResult(NOTHING)
    items: list[tuple[str, Grant]] = [(site_label(s), Grant(sites={s})) for s in sorted(asked.sites)]
    items += [(SEARCH_LABEL, Grant(search=True))] if asked.search else []
    items += [(email_label(p), Grant(email={p})) for p in sorted(asked.email)]
    items += [(CALENDAR_LABEL, Grant(calendar=True))] if asked.calendar else []
    fields = [Field(label, "true", "bool") for label, _ in items]
    decision, refused = await guard.ask(
        ctx, "permissions", "Allow these up front, for this request?", fields, declined=DECLINED
    )
    if decision.reason == "replan":
        return ToolResult(REPLAN)
    if refused:
        return refused
    allowed = [(label, g) for (label, g), v in zip(items, decision.values, strict=False) if v == "true"]
    for _, g in allowed:
        have.add(g)
    left = [label for label, _ in items if label not in {a for a, _ in allowed}]
    if not allowed:
        return ToolResult(DECLINED)
    text = "Allowed up front: " + "; ".join(a for a, _ in allowed) + "."
    if left:
        text += " Not allowed (asked when it happens): " + "; ".join(left) + "."
    return ToolResult(text + " Go ahead.")


def register(registry: Registry) -> None:
    strings = {"type": "array", "items": {"type": "string"}}
    registry.add(
        Tool(
            NAME,
            DESCRIPTION,
            {
                "type": "object",
                "properties": {
                    "sites": {**strings, "description": "Sites to work on: their names, like thefork.co.uk."},
                    "search": {"type": "boolean", "description": "Search the web after reading private things."},
                    "email": {**strings, "description": "Email addresses of the people to email or invite."},
                    "calendar": {"type": "boolean", "description": "Add or change events in their calendar."},
                },
                "required": [],
            },
            permissions_request,
        )
    )
