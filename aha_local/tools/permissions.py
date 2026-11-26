# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Permissions up front (SPEC §3.4 "Permissions up front", §5.2 "Allowed up front").

The first step of every task: the model says what the request will need (sites to open, sites to type on,
searching after reading private things, people to email, drafts, calendar events to add or to change), each
read apart from its write, and the person allows it on one card, all at once,
instead of a card for each action as it happens. What they tick goes into the task's `Grant`, in code; the
agent keeps it for the topic, and a background task started from it keeps it for its checks. Anything not
allowed asks as usual when it happens.
"""

from __future__ import annotations

from ..approvals import Field
from ..policy import grants
from ..policy.grants import Grant, emails_in
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
    "ask for exactly what they need, no more. Reading and writing are asked apart: ask the write only when you "
    "will write. open: sites whose pages you'll only read after reading their email or a signed-in page "
    "(reading a site otherwise needs nothing). type: sites you'll type into (a search box, a form, a booking, "
    "a sign-up, a purchase); it includes opening them. search: whether you'll search the web after reading "
    "their email or a signed-in page. email: the people you'll send email to or invite, by address. drafts: "
    "whether you'll save email drafts (nothing is sent). calendar_add: whether you'll add new events. "
    "calendar_change: whether you'll change events already in their calendar. Leave all empty when the request "
    "needs none of these (a question, a search, reading their email). Anything you didn't ask for is asked when "
    "it happens. Call it again later only if the plan changed."
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
    opens, bad_opens = _sites(args.get("open"))
    types, bad_types = _sites(args.get("type"))
    people, bad_people = _people(args.get("email"))
    if bad_opens or bad_types or bad_people:
        wrong = ", ".join([*bad_opens, *bad_types, *bad_people])
        return ToolResult(
            f"Not asked: {wrong} isn't a site's name (like thefork.co.uk) or one email address. Fix it and call "
            f"{NAME} again.",
            error=True,
        )
    if len(set(opens) | set(types)) > MAX_ITEMS or len(people) > MAX_ITEMS:
        return ToolResult(f"Not asked: at most {MAX_ITEMS} sites and {MAX_ITEMS} people. Ask for fewer.", error=True)
    ctx.task.estimated = True
    have, free = ctx.task.grant, set(trusted.sites(ctx.account)) if ctx.account is not None else set()
    yes = lambda key: args.get(key) is True  # noqa: E731
    # What isn't allowed yet: trusted sites never ask, and what the topic already allowed stays allowed. Typing
    # on a site includes opening it, so a site asked for both shows once, as typing.
    new_types = {s for s in types if s not in free and not have.types(s)}
    asked = Grant(
        {s for s in opens if s not in free and not have.opens(s) and s not in new_types},
        new_types,
        yes("search") and not have.search,
        {p for p in people if p not in have.email},
        yes("drafts") and not have.drafts,
        yes("calendar_add") and not have.calendar_add,
        yes("calendar_change") and not have.calendar_change,
    )
    if not asked:
        return ToolResult(NOTHING)
    items: list[tuple[str, Grant]] = [(grants.type_label(s), Grant(type={s})) for s in sorted(asked.type)]
    items += [(grants.open_label(s), Grant(open={s})) for s in sorted(asked.open)]
    items += [(grants.SEARCH_LABEL, Grant(search=True))] if asked.search else []
    items += [(grants.email_label(p), Grant(email={p})) for p in sorted(asked.email)]
    items += [(grants.DRAFTS_LABEL, Grant(drafts=True))] if asked.drafts else []
    items += [(grants.CALENDAR_ADD_LABEL, Grant(calendar_add=True))] if asked.calendar_add else []
    items += [(grants.CALENDAR_CHANGE_LABEL, Grant(calendar_change=True))] if asked.calendar_change else []
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
                    "open": {**strings, "description": "Sites whose pages you'll only read, after private things."},
                    "type": {**strings, "description": "Sites you'll type or fill in forms on, like thefork.co.uk."},
                    "search": {"type": "boolean", "description": "Search the web after reading private things."},
                    "email": {**strings, "description": "Email addresses of the people to send email to or invite."},
                    "drafts": {"type": "boolean", "description": "Save email drafts (nothing is sent)."},
                    "calendar_add": {"type": "boolean", "description": "Add new events to their calendar."},
                    "calendar_change": {"type": "boolean", "description": "Change events already there."},
                },
                "required": [],
            },
            permissions_request,
        )
    )
