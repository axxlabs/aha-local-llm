# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The page as the harness reads it (SPEC §3.4): snapshots, the current address, element labels, cookie
banners, sign-in forms and "are you human?" checks, and the notes the model gets about them."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import mcp.types as mt

from ..items import Image
from ..policy.urls import site_of
from . import human, signins
from .page_view import for_model
from .registry import ToolContext, ToolResult

log = logging.getLogger("aha.browser")

HTTP_STATUS = re.compile(r"^- HTTP status: (\d{3})", re.M)
PAGE_URL = re.compile(r"^- Page URL: (\S+)", re.M)
# A sign-in form on the page, from the page's own snapshot (SPEC §3.4 "Signing in"): a password field, or an email
# or username field with a "Sign in" / "Log in" button or heading, or on a sign-in address.
PASSWORD_FIELD = re.compile(
    r'^\s*- textbox "[^"]*(?:password|passcode|passwort|mot de passe|senha|contrase)', re.I | re.M
)
USER_FIELD = re.compile(r'^\s*- textbox "[^"]*(?:e-?mail|username|user name|user id|login)', re.I | re.M)
SIGN_IN_CONTROL = re.compile(r'^\s*- (?:button|heading)\b[^\n]*"[^"]*\b(?:sign ?in|log ?in|login)\b', re.I | re.M)
SIGN_IN_ADDRESS = re.compile(r"/(?:log-?in|sign-?in|signin|sign_in|auth(?:enticate)?)(?:[/?#.]|$)", re.I)
# An "are you human?" check on the page (SPEC §3.4 "When a site still checks"), from the page's own snapshot.
CHALLENGE = re.compile(
    r"i['’]?m not a robot|verify (?:that )?(?:you are|you['’]re) (?:a )?human|are you (?:a )?(?:robot|human)|"
    r"press (?:&|and) hold|captcha|unusual traffic|security challenge|checking your browser|"
    r"checking if the site connection is secure|confirm you are (?:a )?human",
    re.I,
)
# A cookie banner (SPEC §3.4 "Cookie banners"): a short button that turns the optional cookies down, near the
# word "cookie" in the page's own snapshot. The harness clicks it before the model or a card sees the page.
REJECT_BUTTON = re.compile(r'^\s*- button "([^"]{1,40})"[^\n]*\[ref=((?:f\d+)?e\d+)\]', re.M)
REJECT_NAME = re.compile(
    r"^(?:reject|decline|refuse|deny|(?:use |allow |accept )?(?:only )?(?:strictly )?(?:necessary|essential)\b|"
    r"only (?:allow|accept|use) (?:strictly )?(?:necessary|essential)\b|continue without accepting)",
    re.I,
)
COOKIE_NEAR = 40  # lines around the button where the banner says "cookie"
CANT_TELL = (
    "From here you can't tell why: don't guess at a cause (region, IP, outage) the page didn't state, and don't "
    "try another address you put together. Open the site's home page and find the page from there (its menus, "
    "links or search box), or search the web for it. Only if that fails too, tell the person you couldn't load "
    "it and give the link."
)


def to_result(r: mt.CallToolResult) -> ToolResult:
    texts, images = [], []
    for c in r.content:
        if isinstance(c, mt.TextContent):
            texts.append(c.text)
        elif isinstance(c, mt.ImageContent) and c.mimeType in ("image/png", "image/jpeg", "image/webp"):
            images.append(Image(mime=c.mimeType, b64=c.data))
    text = "\n".join(texts)
    status = http_status(text)
    if status and status >= 400:
        text = f"The site answered HTTP {status}. {CANT_TELL}\n{text}"
    return ToolResult(text, images, kind="page", error=bool(r.isError), full=text)


def http_status(text: str) -> int | None:
    """The page's HTTP status as Playwright MCP reports it, if it did."""
    m = HTTP_STATUS.search(text)
    return int(m.group(1)) if m else None


async def current_url(session) -> str:
    """The address of the page the browser is on, read from the browser itself (not from the model)."""
    r = await session.call("browser_tabs", {"action": "list"})
    for c in r.content:
        if isinstance(c, mt.TextContent):
            for line in c.text.splitlines():
                if "(current)" in line and "http" in line:
                    return line[line.index("http") :].split()[0].strip("()[]")
    return ""


async def current_site(session) -> str:
    """The site of the page the browser is on, read from the browser itself (not from the model)."""
    host = urlsplit(await current_url(session)).hostname or ""
    return site_of(host) if host else ""


SNAPSHOT_LINK = re.compile(r"### Snapshot\s*\n- \[Snapshot\]\([^)]*\)")
# The model writes references as "e23", "[ref=e23]", "ref=f8e31"…: the browser wants "e23" / "f8e31".
REF_TARGET = re.compile(r"^[\[(]?\s*(?:ref\s*[=:]\s*)?((?:f\d+)?e\d+)\s*[\])]?$")
# A whole line of the page view, or of a snapshot, given as the target: its reference, in brackets after the name
# (seen: `textbox "Type a message to Meta AI" (f146e910)` sent three times, refused each time).
REF_AT_END = re.compile(r"[\[(]\s*(?:ref\s*[=:]\s*)?((?:f\d+)?e\d+)\s*[\])]")
# A field only the sign-in card may fill: its page label says password or code (SPEC §3.4 "Signing in").
SECRET_FIELD = re.compile(r"pass ?word|passcode|\bpin\b|one[- ]time|verification code|security code|\botp\b|2fa", re.I)
YAML_BLOCK = re.compile(r"```yaml\n(.*?)(?:```|\Z)", re.S)  # tolerant of a snapshot cut to fit


def normalise_targets(args: dict) -> dict:
    """The model often writes "[ref=e23]", or the element's whole line, where the browser wants "e23"."""

    def fix(v):
        if not isinstance(v, str):
            return v
        if m := REF_TARGET.match(v.strip()):
            return m.group(1)
        refs = REF_AT_END.findall(v)
        return refs[-1] if refs else v

    out = {k: fix(v) if k in ("target", "ref") else v for k, v in args.items()}
    if isinstance(out.get("fields"), list):
        out["fields"] = [{**f, "target": fix(f.get("target"))} if isinstance(f, dict) else f for f in out["fields"]]
    return out


def element_label(snapshot: str, ref: str) -> str:
    """The element as the page describes it, e.g. 'searchbox "Search Wikipedia"' (not the model's words)."""
    for line in snapshot.splitlines():
        if f"[ref={ref}]" in line:
            label = re.sub(r"\[[^\]]*\]", "", line).strip().lstrip("- ").rstrip(":").strip()
            if m := re.match(r'^[\w-]+\s+"(.+)"', label):  # 'textbox "Customer name:"' → 'Customer name'
                label = m.group(1).strip().rstrip(":").strip()
            return label[:80]
    return ""


async def call_with_snapshot(session, name: str, args: dict, ctx: ToolContext) -> ToolResult:
    """Run a browser tool; when the result only links to its snapshot file, fetch the snapshot inline."""
    result = to_result(await session.call(name, args))
    if name != "browser_navigate" and (last := blank_window(ctx, result)):
        # A new window starts blank (the browser restarted): the page that was open goes back, in code (SPEC §3.4).
        from . import guard

        reopened = await call_with_snapshot(session, "browser_navigate", {"url": last}, ctx)
        if not reopened.error:
            guard.opened(ctx, last)
            reopened.text = f"The browser had restarted, so SHA opened the page that was open last.\n\n{reopened.text}"
            return reopened
    if SNAPSHOT_LINK.search(result.text):
        snap = to_result(await session.call("browser_snapshot", {}))
        if not snap.error:
            result.full = SNAPSHOT_LINK.sub(
                lambda _: "### Snapshot\n" + snap.full.split("### Snapshot", 1)[-1].strip(), result.full or result.text
            )
            result.text = SNAPSHOT_LINK.sub(
                lambda _: "### Snapshot\n" + snap.text.split("### Snapshot", 1)[-1].strip(), result.text
            )
    await close_cookie_banner(session, ctx, result)
    annotate(ctx, result)
    for_model(ctx, result)
    return result


def cookie_banner(snapshot: str) -> tuple[str, str] | None:
    """The reference and name of a cookie banner's "reject" button in a snapshot, if the page shows one."""
    lines = snapshot.splitlines()
    for i, line in enumerate(lines):
        m = REJECT_BUTTON.match(line)
        if not m or not REJECT_NAME.match(m.group(1).strip()):
            continue
        if any("cookie" in near.lower() for near in lines[max(0, i - COOKIE_NEAR) : i + COOKIE_NEAR]):
            return m.group(2), m.group(1).strip()
    return None


async def close_cookie_banner(session, ctx: ToolContext, result: ToolResult) -> None:
    """Close a cookie banner in code, turning the optional cookies down (SPEC §3.4 "Cookie banners").

    Left to the model, a banner could stay over the page, and a sign-in card's typing then lands on it. Tried once
    per page and button; the page the model gets is the one after. Looked for in the whole page: a banner is often
    the last thing on it, past where a long page is cut to fit (seen on evri.com)."""
    m = YAML_BLOCK.search(result.full or result.text)
    found = cookie_banner(m.group(1)) if m else None
    if found is None:
        return
    ref, label = found
    url = (u.group(1) if (u := PAGE_URL.search(result.text)) else "") or ctx.task.page_url
    if (url, ref) in ctx.task.banners_tried:
        return
    ctx.task.banners_tried.add((url, ref))
    try:
        await run_code(session, human.move_code(ref))
        click = await session.call("browser_click", {"element": f'"{label}" on the cookie banner', "target": ref})
        snap = to_result(await session.call("browser_snapshot", {}))
    except Exception:
        log.warning("couldn't close a cookie banner")
        return
    if click.isError or snap.error or "### Page" not in snap.text:
        log.info("closing a cookie banner didn't work")
        return
    log.info("closed a cookie banner")
    head = result.text.split("### Page", 1)[0]
    result.text = (
        f'[SHA: a cookie banner was over this page; it was closed with "{label}". This is the page now.]\n\n'
        + head
        + "### Page"
        + snap.text.split("### Page", 1)[1]
    )
    result.full = result.text  # the page after the banner, whole, for the harness (refs, links)


REF_IN_LINE = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")


def remember_refs(ctx: ToolContext, text: str) -> None:
    """The elements seen on the current page (snapshots and finds), so a card is never shown for one that isn't."""
    m = PAGE_URL.search(text)
    if m and m.group(1) != ctx.task.page_url:
        ctx.task.page_url, ctx.task.ref_lines = m.group(1), {}
    for line in text.splitlines():
        for ref in REF_IN_LINE.findall(line):
            ctx.task.ref_lines[ref] = line


def annotate(ctx: ToolContext, result: ToolResult) -> None:
    """What the harness tells the model about a page it opened, in code (SPEC §3.4, §5.6)."""
    remember_refs(ctx, result.text)
    if m := YAML_BLOCK.search(result.text):
        ctx.task.last_snapshot = m.group(1)
        note_sign_in_form(ctx, result, m.group(1))
        note_challenge(ctx, result, m.group(1))


def blank_window(ctx: ToolContext, result: ToolResult) -> str:
    """The page that was open last, when this result is a new window's blank page; "" otherwise."""
    m = PAGE_URL.search(result.text)
    if not m or m.group(1) != "about:blank" or ctx.account is None:
        return ""
    return signins.last_page(ctx.account)


def note_challenge(ctx: ToolContext, result: ToolResult, snapshot: str) -> None:
    """An "are you human?" check on the page: noted for the end-of-request check (SPEC §3.5.1)."""
    m = PAGE_URL.search(result.text)
    host = urlsplit(m.group(1)).hostname or "" if m else ""
    if not host or not CHALLENGE.search(snapshot):
        ctx.task.challenge_page = ""
        return
    ctx.task.challenge_page = site_of(host)
    # Challenge pages often answer 403 or 429: that's the check, not a page that won't load.
    result.text = re.sub(r"^The site answered HTTP \d{3}\. " + re.escape(CANT_TELL) + "\n", "", result.text)
    result.error = False


def is_sign_in_form(snapshot: str, url: str) -> bool:
    if PASSWORD_FIELD.search(snapshot):
        return True
    return bool(USER_FIELD.search(snapshot)) and bool(
        SIGN_IN_CONTROL.search(snapshot) or SIGN_IN_ADDRESS.search(urlsplit(url).path)
    )


def note_sign_in_form(ctx: ToolContext, result: ToolResult, snapshot: str) -> None:
    """A sign-in form on the page: noted for the end-of-request check (SPEC §3.5.1). The page is what counts,
    not an earlier sign-in: a session the browser remembered can have ended."""
    m = PAGE_URL.search(result.text)
    url = m.group(1) if m else ""
    host = urlsplit(url).hostname or ""
    ctx.task.sign_in_page = site_of(host) if host and is_sign_in_form(snapshot, url) else ""


async def run_code(session, code: str) -> bool:
    """Run fixed harness code (SPEC §3.4). Its result echoes the code, so it's never passed on."""
    try:
        return human.ran(await session.call("browser_run_code_unsafe", {"code": code}))
    except Exception:
        log.warning("harness browser code failed")
        return False


async def human_type(session, ref: str, text: str, submit: bool, newline: str = "Enter") -> bool:
    """Type into a field one key at a time (SPEC §3.4). False if it couldn't: the caller falls back."""
    if not human.is_ref(ref) or len(text) > human.MAX_HUMAN_TYPING:
        return False
    return await run_code(session, human.type_code(ref, text, submit, newline=newline))


async def screenshot(session) -> Image | None:
    try:
        r = to_result(await session.call("browser_take_screenshot", {"type": "jpeg"}))
    except Exception:
        log.warning("couldn't take a screenshot")
        return None
    return r.images[0] if r.images else None
