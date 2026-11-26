# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The browser: Playwright MCP behind the policy (SPEC §3.4, §5.2).

One browser per person, each with its own profile folder in the encrypted mount (SPEC §3.4 "Browser profiles"),
started by aha-browser's supervisor; one MCP connection to each: tabs, cookies, storage and logins never cross
between people. The browser keeps everything itself, so nothing is exported or put back. A task only ever reaches
its own account's browser, through `ctx.browser`; the model can't name another. A connection opens on the
account's first browser call and keeps its page between tasks; Clear browser and `aha delete` delete the profile.

Each browser tool the model sees is wrapped: the policy decides auto, approval or block from the real
arguments; approvals are answered only by the person (approvals.py).
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from .. import messages
from ..policy.browser import TYPING, classify, form_for, is_message_box, offered, opens
from ..policy.gate import explain
from ..policy.urls import site_of
from . import guard, human
from .browser_page import (
    SECRET_FIELD,
    call_with_snapshot,
    current_site,
    current_url,
    element_label,
    human_type,
    normalise_targets,
    run_code,
    screenshot,
    to_result,
)
from .browser_pool import BrowserPool
from .browser_signin import browser_human_check, browser_login
from .page_view import browser_read_more
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

log = logging.getLogger("aha.browser")
log = logging.getLogger("aha.browser")


async def run_approved(session, name: str, args: dict, ctx: ToolContext) -> ToolResult:
    """Run an approved action; typing goes at human pace where it can (SPEC §3.4)."""
    typing = name == "browser_type" and not args.get("slowly") and isinstance(args.get("text"), str)
    # In a message box Enter often sends: a new line in the text is Shift+Enter there (SPEC §3.4).
    newline = "Shift+Enter" if any(is_message_box(label) for label in labels_of(name, args, ctx)) else "Enter"
    if typing and await human_type(
        session, str(args.get("target", "")), args["text"], bool(args.get("submit")), newline
    ):
        return await call_with_snapshot(session, "browser_snapshot", {}, ctx)
    if name == "browser_fill_form" and isinstance(args.get("fields"), list):
        # In the form's order: text fields typed one key at a time, the others (checkboxes, lists) filled by the
        # MCP in between, so a choice that reveals a later field still comes first.
        pending: list = []
        for f in args["fields"]:
            text_field = isinstance(f, dict) and f.get("type") == "textbox" and isinstance(f.get("value"), str)
            if text_field and human.is_ref(f.get("target")) and len(f["value"]) <= human.MAX_HUMAN_TYPING:
                if pending:
                    filled = await call_with_snapshot(session, name, {**args, "fields": pending}, ctx)
                    if filled.error:
                        return filled
                    pending = []
                if await human_type(session, f["target"], f["value"], False):
                    continue
            pending.append(f)
        if pending:
            return await call_with_snapshot(session, name, {**args, "fields": pending}, ctx)
        return await call_with_snapshot(session, "browser_snapshot", {}, ctx)
    return await call_with_snapshot(session, name, args, ctx)


def check_targets(name: str, args: dict, ctx: ToolContext) -> ToolResult | None:
    """Typing goes only into elements of the latest snapshot, and never into a password or code field."""
    spec = TYPING.get(name)
    if spec is None or not spec.checks_targets:
        return None
    for ref in spec.targets(args):
        if ref not in ctx.task.ref_lines:
            return ToolResult(
                f"{ref or 'That element'} isn't in the latest snapshot. Take a snapshot and give the element's "
                "reference exactly as shown after ref=, like e12 or f8e31.",
                error=True,
            )
        label = element_label(ctx.task.ref_lines[ref], ref)
        if SECRET_FIELD.search(label):
            return ToolResult(
                f"“{label}” is a sign-in field: never fill it yourself. Call browser_login instead: it signs the user "
                "in, and you never see their details.",
                error=True,
            )
    return None


def labels_of(name: str, args: dict, ctx: ToolContext) -> list[str]:
    """The page's own labels of the elements a typing call targets, from the latest snapshot."""
    spec = TYPING.get(name)
    return [element_label(ctx.task.last_snapshot, ref) for ref in spec.targets(args)] if spec else []


def labelled(name: str, args: dict, ctx: ToolContext) -> dict:
    """Replace the model's element descriptions with the page's own labels, for the approval."""
    snap = ctx.task.last_snapshot
    out = dict(args)
    if name in ("browser_type", "browser_select_option") and (
        label := element_label(snap, str(args.get("target", "")))
    ):
        out["element"] = label
    if name == "browser_fill_form" and isinstance(args.get("fields"), list):
        fields = []
        for f in args["fields"]:
            label = element_label(snap, str(f.get("target", ""))) if isinstance(f, dict) else ""
            fields.append({**f, "name": label} if label else f)
        out["fields"] = fields
    return out


def make_handler(name: str):
    async def handler(args: dict, ctx: ToolContext) -> ToolResult:
        session = ctx.browser  # this account's own context, never another's (SPEC §3.4)
        if session is None:
            return ToolResult("The browser isn't available right now.", error=True)
        args = normalise_targets(args)
        if refused := check_targets(name, args, ctx):  # before any card: a call that would fail never asks
            return refused
        if opens(name, args):  # the same rules as fetch_url (SPEC §5.2 rules 5–7)
            url, action, refused = await guard.open_address(ctx, str(args.get("url", "")), name)
            return refused or await ran(session, name, {**args, "url": url}, action, ctx)
        site = ""
        if classify(name, args).kind in ("type", "other"):
            try:
                site = await current_site(session)  # the browser's own address, never the model's
            except Exception:
                log.warning("couldn't read the current site")
        action = classify(name, args, site, labels_of(name, args, ctx))
        verdict = guard.check(ctx, action, name)  # SPEC §5.2: the one place that decides
        if verdict.outcome == "block":
            return guard.blocked(verdict.why)
        if verdict.outcome == "run":
            if action.kind == "read":
                clicked = clicked_on(args, ctx) if name == "browser_click" else ""  # before the page changes
                if name == "browser_click" and human.is_ref(args.get("target")):
                    await run_code(session, human.move_code(args["target"]))  # the mouse gets there first (§3.4)
                result = await call_with_snapshot(session, name, args, ctx)
                if clicked and not result.error:
                    result.did = clicked
                return result
            return await ran(session, name, args, action, ctx)
        try:  # the card shows the page the action happens on
            shot_result = to_result(await session.call("browser_take_screenshot", {}))
            shot = shot_result.images[0] if shot_result.images else None
        except Exception:
            log.warning("couldn't take a screenshot for the approval")
            shot = None
        signer = ctx.account.label if action.kind == "send" else ""
        form = form_for(name, labelled(name, args, ctx), action.site, signer)
        why = explain(verdict.why, action.site)
        decision, refused = await guard.ask(ctx, "action", form.title, form.fields, shot, why=why)
        if refused:
            return refused
        return await ran(session, name, form.apply(name, args, decision.values), action, ctx)

    return handler


async def ran(session, name: str, args: dict, action, ctx: ToolContext) -> ToolResult:
    """Run an action that may go out (it passed the gate, with or without a card), and say what went, in the same
    words either way (SPEC §5.1)."""
    what = entered(name, args, ctx)  # before the page changes: its labels are this page's
    result = await run_approved(session, name, args, ctx)
    if action.kind == "open":
        if not result.error:
            guard.opened(ctx, str(args.get("url", "")))
        return result
    if what:
        result.text = what + "\n\n" + result.text
    if not result.error:  # the request's record (SPEC §4 "What a request did")
        result.did = (what or name.removeprefix("browser_")) + (f" (on {action.site})" if action.site else "")
    return result


def clicked_on(args: dict, ctx: ToolContext) -> str:
    """A click for the request's record (SPEC §4): the page's own label of what it clicks, and the site. A click
    may change things (a basket, a booking), so a later request must know it happened."""
    ref = str(args.get("target", ""))
    label = element_label(ctx.task.ref_lines.get(ref, ""), ref) or " ".join(str(args.get("element", "")).split())
    host = urlsplit(ctx.task.page_url).hostname or ""
    site = site_of(host) if host else ""
    return f"Clicked “{label}”" + (f" on {site}" if site else "")


SIGNED_NOTE = (
    " The signature at its end was added by Aha! on purpose and approved by the person with the text: leave it "
    "as it is, and don't mention it."
)


def entered(name: str, args: dict, ctx: ToolContext) -> str:
    """What was typed or chosen, as plain fact (the person may have changed it on a card; the model can't tell).
    A signed message says the signature is meant (seen: the model took it for a mistake and tried to have the
    person delete it on a human-check card)."""
    spec = TYPING.get(name)
    what = spec.entered(args, labels_of(name, args, ctx)) if spec else ""
    return what + SIGNED_NOTE if what and messages.ALREADY_SIGNED.search(what) else what


async def show_screenshot(args: dict, ctx: ToolContext) -> ToolResult:
    """Send the person a screenshot of their browser's current page (SPEC §3.4)."""
    if ctx.browser is None or ctx.send is None:
        return ToolResult("The browser isn't available right now.", error=True)
    if not ctx.browser.is_open() or not (await current_url(ctx.browser)).startswith("http"):
        return ToolResult("No page is open in the browser: open one with browser_navigate first.", error=True)
    shot = await screenshot(ctx.browser)
    if shot is None:
        return ToolResult(
            "The browser didn't return a screenshot. Try once more after the page has loaded.", error=True
        )
    caption = " ".join(str(args.get("caption", "") or "").split())[:300]
    await ctx.send(caption, shot)
    return ToolResult("Sent the screenshot to the person's chat. Carry on; don't describe it unless they ask.")


# Sign-in fields, found by the harness from the page's own labels (SPEC §5.8): the model names none of them.

SEEN_ONLY_BY_YOU = {
    "browser_take_screenshot": " Only you see this screenshot; to show the page to the user, use show_screenshot."
}

HARNESS_TOOLS = (
    Tool(
        "show_screenshot",
        "Send the user a screenshot of the browser's current page in the chat, so they see it themselves. Use it "
        "when they ask to see a page or a screenshot, or when a result is clearer as a picture (a map, a seat "
        "plan, a confirmation). Describing a page is not the same as showing it.",
        params(caption=string("A short caption, optional")),
        show_screenshot,
    ),
    Tool(
        "browser_read_more",
        "Read the next part of a long page: the page says when it has more parts. Read on before you say "
        "something isn't on the page.",
        params(["part"], part={"type": "integer", "description": "The part to read, e.g. 2"}),
        browser_read_more,
    ),
    Tool(
        "browser_login",
        "When the page asks the user to sign in (or for a one-time code), sign them in: call it on the page with "
        "the sign-in form. You never see their details. Returns whether they're signed in now and the page.",
        params(),
        browser_login,
    ),
    Tool(
        "browser_human_check",
        'When the page shows an "are you human?" check you can\'t pass with one click (a picture puzzle, "verify '
        'you are human", a check that came back), let the user do it: they see the page and tap where to '
        "click. Never try to solve a picture puzzle yourself.",
        params(),
        browser_human_check,
    ),
)


async def register(reg: Registry, pool: BrowserPool, wait: float = 60) -> int:
    """The browser's tools, as its image defines them: the image is pinned by digest (SPEC §9.2), so a change is a
    reviewed commit."""
    try:
        tools = await pool.tools(wait)
    except TimeoutError:
        log.warning("browser not reachable; browsing is unavailable until restart")
        return 0
    count = 0
    for t in tools:
        if not offered(t.name):
            continue
        reg.add(
            Tool(t.name, (t.description or "") + SEEN_ONLY_BY_YOU.get(t.name, ""), t.inputSchema, make_handler(t.name))
        )
        count += 1
    if count:
        for tool in HARNESS_TOOLS:
            reg.add(tool)
    log.info("browser: %d tools, one context per account", count)
    return count
