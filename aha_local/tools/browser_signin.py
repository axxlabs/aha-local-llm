# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Signing in and human checks, done by the person on cards (SPEC §3.4 "Signing in", §5.8): the model never
sees what they type."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import re
import struct
from dataclasses import dataclass
from dataclasses import field as dc_field
from urllib.parse import urlsplit

from ..approvals import Field
from ..items import Image
from ..policy.urls import check_url
from . import guard, human, logins, signins
from .browser_page import call_with_snapshot, current_url, human_type, is_sign_in_form, run_code, screenshot
from .registry import ToolContext, ToolResult, redact

log = logging.getLogger("aha.browser")

LOGIN_BOX = re.compile(r'^\s*- textbox "([^"]*)"[^\n]*?\[ref=((?:f\d+)?e\d+)\]', re.M)
CODE_LABEL = re.compile(r"one[- ]time|verification|security code|\bcode\b|\botp\b|2fa", re.I)
PASSWORD_LABEL = re.compile(r"pass ?word|passcode|passwort|mot de passe|senha|contrase|\bpin\b", re.I)
USER_LABEL = re.compile(r"e-?mail|user ?name|user id|log ?in|sign ?in|phone|mobile|account", re.I)
SAVE_LABEL = "Save this login on this computer"
WAIT_AFTER_SUBMIT = 2.5


@dataclass
class LoginFields:
    username: str = ""
    passwords: list[str] = dc_field(default_factory=list)  # a sign-up page also has "confirm password"
    code: str = ""

    def __bool__(self) -> bool:
        return bool(self.username or self.passwords or self.code)


def login_fields(snapshot: str) -> LoginFields:
    found = LoginFields()
    for label, ref in LOGIN_BOX.findall(snapshot):
        if CODE_LABEL.search(label) and not PASSWORD_LABEL.search(label):
            found.code = found.code or ref
        elif PASSWORD_LABEL.search(label):
            found.passwords.append(ref)
        elif USER_LABEL.search(label) and not found.username:
            found.username = ref
    return found


async def type_secret(session, ref: str, value: str, submit: bool) -> bool:
    """Type a login value into the page. The tool's own result echoes what was typed: it's never passed on."""
    if await human_type(session, ref, value, submit):  # one key at a time, like a person (§3.4)
        return True
    r = await session.call("browser_type", {"element": "sign-in field", "target": ref, "text": value, "submit": submit})
    return not r.isError


async def look(session, ctx: ToolContext, wait: bool = True) -> tuple[ToolResult, LoginFields]:
    """The page now (after a submit, once it had time to load) and its sign-in fields."""
    if wait:
        with contextlib.suppress(Exception):
            await session.call("browser_wait_for", {"time": WAIT_AFTER_SUBMIT})
    page = await call_with_snapshot(session, "browser_snapshot", {}, ctx)
    return page, login_fields(ctx.task.last_snapshot)


async def browser_login(args: dict, ctx: ToolContext) -> ToolResult:
    """Sign the person in on the current page (SPEC §5.8): a saved login, or a login card; a code card if asked.

    The model gives nothing and learns only whether they're signed in now."""
    session = ctx.browser
    if session is None:
        return ToolResult("The browser isn't available right now.", error=True)
    ctx.task.login_tried = True
    url = await current_url(session) if session.is_open() else ""
    site, why = check_url(url)
    if why or not site:
        return guard.blocked(why or "no page is open")
    if urlsplit(url).scheme != "https":
        return ToolResult("Signing in only works on secure (https) pages. Tell the user.", error=True)
    # The exact host: on shared hosting (x.herokuapp.com, y.github.io) the site alone would let a look-alike page
    # borrow a trusted name, and a saved login is only ever typed where it was saved.
    host = (urlsplit(url).hostname or site).lower().removeprefix("www.")
    page, fields = await look(session, ctx, wait=False)
    if not (is_sign_in_form(ctx.task.last_snapshot, url) or fields.code):
        return ToolResult(
            "There's no sign-in form on this page. Open the site's sign-in page (or its account page) first.",
            error=True,
        )
    if fields.username or fields.passwords:
        saved = None if host in ctx.task.saved_login_failed else logins.get(ctx.account, host)
        if saved:
            username, password, keep = saved.get("username", ""), saved["password"], False
        else:
            card = [Field("Site", host, "info")]
            first = len(card)
            card += [Field("Email or username", "", "text"), Field("Password", "", "secret")]
            card.append(Field(SAVE_LABEL, "false", "bool"))
            decision, refused = await guard.ask(
                ctx, "signin", f"Sign in to {host}?", card, await screenshot(session), "Don't ask for it in the chat."
            )
            if refused:
                return refused
            username, password = decision.values[first], decision.values[first + 1]
            keep = decision.values[first + 2] == "true"
        if password:
            ctx.task.secrets.append(password)
        if (
            fields.username
            and username
            and not await type_secret(session, fields.username, username, submit=not fields.passwords)
        ):
            return ToolResult("Typing into the sign-in form failed. Take a snapshot and try again.", error=True)
        if not fields.passwords and password:  # a two-step login: the password comes on the next page
            page, fields = await look(session, ctx)
        for i, ref in enumerate(fields.passwords if password else []):
            if not await type_secret(session, ref, password, submit=i == len(fields.passwords) - 1):
                return ToolResult("Typing into the sign-in form failed. Take a snapshot and try again.", error=True)
        page, fields = await look(session, ctx)
        if saved and fields.passwords:  # back at a sign-in form: the saved login no longer works
            ctx.task.saved_login_failed.add(host)
            log.info("a saved login led back to a sign-in form; asking the person")
            return await browser_login(args, ctx)
        if keep and password and not fields.passwords:
            logins.save(ctx.account, host, username, password)
    if fields.code and not fields.passwords:  # a one-time code: asked every time, never saved
        decision, refused = await guard.ask(
            ctx,
            "signin",
            f"Enter the code for {host}?",
            [Field("Site", host, "info"), Field("Code", "", "secret")],
            await screenshot(session),
            "Don't ask for it in the chat.",
        )
        if refused:
            return refused
        code = decision.values[1].strip()
        ctx.task.secrets.append(code)
        if not await type_secret(session, fields.code, code, submit=True):
            return ToolResult("Typing into the sign-in form failed. Take a snapshot and try again.", error=True)
        page, fields = await look(session, ctx)
    signed = not is_sign_in_form(ctx.task.last_snapshot, await current_url(session))
    ctx.task.last_snapshot = redact(ctx.task.last_snapshot, ctx.task.secrets)
    said = f"Signed in on {host}." if signed else f"Not signed in on {host}: look at the page for why."
    page.text = said + " The page now:\n\n" + page.text  # the registry hides what they typed (SPEC §5.8)
    return page


CHECK_ROUNDS = 15
GAVE_UP = (
    "The person gave up on the check. Tell them you couldn't get past it and give the link; don't try the check "
    "yourself."
)


def image_size(image: Image) -> tuple[int, int] | None:
    """Width and height of a PNG or JPEG screenshot, from its header."""
    try:
        b = base64.b64decode(image.b64)
    except ValueError:
        return None
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24:
        return struct.unpack(">II", b[16:24])
    i = 2
    while b[:2] == b"\xff\xd8" and i + 9 <= len(b):
        marker, length = b[i + 1], struct.unpack(">H", b[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", b[i + 5 : i + 9])
            return w, h
        i += 2 + length
    return None


async def page_size(session) -> tuple[int, int] | None:
    """The page's size in CSS pixels, where the mouse moves (fixed harness code)."""
    try:
        r = await session.call(
            "browser_run_code_unsafe",
            {"code": "async (page) => JSON.stringify(await page.evaluate(() => [innerWidth, innerHeight]))"},
        )
        w, h = json.loads(signins._result(r))
        return int(w), int(h)
    except Exception:
        return None


NO_CHECK = (
    'No "are you human?" check was found on this page. This tool is only for those checks (a picture puzzle, '
    '"verify you are human"), never to edit text or click something on the page. If a check really is on the '
    "page, call it again."
)


async def browser_human_check(args: dict, ctx: ToolContext) -> ToolResult:
    """The person does a site's "are you human?" check on a card: they tap the screenshot (SPEC §3.4)."""
    session = ctx.browser
    if session is None:
        return ToolResult("The browser isn't available right now.", error=True)
    url = await current_url(session) if session.is_open() else ""
    if not url.startswith("http"):
        return ToolResult("No page is open in the browser: open one with browser_navigate first.", error=True)
    site, why = check_url(url)
    if why or not site:
        return guard.blocked(why)
    shown = (urlsplit(url).hostname or site).lower().removeprefix("www.")
    if not ctx.task.challenge_page and not ctx.task.human_check_asked:
        # Seen: with no check on WhatsApp, the model called this to have the person tap away a signature. The
        # detector can miss a check (a puzzle in a frame), so asking again in the task shows the card.
        ctx.task.human_check_asked = True
        return ToolResult(NO_CHECK, error=True)
    ctx.task.human_check_offered = True
    taps_done = 0
    for _ in range(CHECK_ROUNDS):
        shot = await screenshot(session)
        size = image_size(shot) if shot else None
        if shot is None or size is None:
            return ToolResult("The browser didn't return a screenshot. Try once more.", error=True)
        fields = [Field("Site", shown, "info"), Field("Taps", "", "taps")]
        decision, refused = await guard.ask(ctx, "check", f"Show {shown} you're human", fields, shot, declined=GAVE_UP)
        if refused:
            return refused
        taps = human.parse_taps(decision.values[1] if len(decision.values) > 1 else "", *size)
        if taps is None:  # outside the screenshot: nothing is clicked; a fresh card
            continue
        if not taps:  # "It's done": look at the page
            break
        page = await page_size(session) or size
        sx, sy = page[0] / size[0], page[1] / size[1]
        points = [(round(x * sx), round(y * sy)) for x, y in taps]
        if not await run_code(session, human.tap_code(points)):
            log.warning("clicking a check card's taps failed")
        taps_done += len(points)
        with contextlib.suppress(Exception):
            await session.call("browser_wait_for", {"time": 1.5})
    page = await call_with_snapshot(session, "browser_snapshot", {}, ctx)
    page.text = (
        f"The person did the check on {shown} ({taps_done} taps) and said it's done. Look at the page: if "
        "the check is still there, tell them you couldn't get past it and give the link. The page now:\n\n" + page.text
    )
    return page


# Added to a browser tool's own description (static, so the prompt prefix stays cached).
