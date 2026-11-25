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

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
import struct
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import mcp.types as mt
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..approvals import Field
from ..items import Image
from ..policy.browser import MESSAGE_SITES, TYPING, classify, form_for, offered, opens
from ..policy.gate import explain
from ..policy.urls import check_url, site_of
from . import guard, human, logins, signins
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

log = logging.getLogger("aha.browser")

MAX_SNAPSHOT_CHARS = 30_000
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
    "From here you can't tell why: tell the person you couldn't load the page and give the link; "
    "don't guess at a cause (region, IP, outage) the page didn't state."
)


class BrowserSession:
    """One MCP connection, owned by a background task so its page survives between calls."""

    def __init__(self, url: str | Callable[[], Awaitable[str]]) -> None:
        self.url = url  # the browser's address, or how to get it (started on demand by the supervisor)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.tools: list[mt.Tool] = []
        self.busy = 0
        self.last_used = time.monotonic()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self, timeout: float = 5) -> None:
        """Close the connection. The person's browser and its profile stay (SPEC §3.4 "Browser profiles")."""
        if not self._task or self._task.done():
            return
        if self.ready.is_set():
            await self.queue.put(None)  # leave the session cleanly, so the browser closes the context now
            done, _ = await asyncio.wait([self._task], timeout=timeout)
            if done:
                return
        self._task.cancel()
        await asyncio.wait([self._task])  # without re-raising the task's own cancellation

    async def _run(self) -> None:
        while True:
            try:
                url = self.url if isinstance(self.url, str) else await self.url()  # again after a lost connection
                async with streamablehttp_client(url) as (r, w, _), ClientSession(r, w) as s:
                    await s.initialize()
                    self.tools = (await s.list_tools()).tools
                    self.ready.set()
                    while True:
                        item = await self.queue.get()
                        if item is None:
                            return
                        fn, fut = item
                        try:
                            fut.set_result(await fn(s))
                        except Exception as e:
                            if not fut.done():
                                fut.set_exception(e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ready.clear()
                log.warning("browser connection lost (%s); retrying in 3 s", type(e).__name__)
                await asyncio.sleep(3)

    async def call(self, name: str, args: dict, timeout: float = 90) -> mt.CallToolResult:
        self.busy += 1
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=30)
            fut = asyncio.get_running_loop().create_future()
            await self.queue.put((lambda s: s.call_tool(name, args), fut))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.busy -= 1
            self.last_used = time.monotonic()


class BrowserPool:
    """One browser per person, each with a real profile of its own (SPEC §3.4 "Browser profiles"), started by the
    browser container's supervisor; one MCP connection to each. The profile is the browser's own folder, in the
    encrypted mount: nothing is exported or put back, so a site keeps everything it stores (seen: WhatsApp Web's
    keys lost by an exported copy, and its link with them)."""

    def __init__(self, url: str, idle_seconds: float, session=BrowserSession, supervise=None) -> None:
        self.url = url.rstrip("/")  # the supervisor
        self.idle_seconds = idle_seconds
        self._session = session
        self._supervise = supervise or self._post
        self._sessions: dict[str, BrowserSession] = {}
        self._held: dict[str, int] = {}
        self._reaper: asyncio.Task | None = None

    async def _post(self, path: str, account_id: str) -> dict:
        async with httpx.AsyncClient(timeout=60) as http:
            r = await http.post(f"{self.url}{path}", json={"id": account_id})
        r.raise_for_status()
        return r.json()

    def _address(self, account_id: str):
        async def address() -> str:
            port = (await self._supervise("/start", account_id))["port"]
            return f"http://{urlsplit(self.url).hostname}:{int(port)}/mcp"

        return address

    async def tools(self, wait: float = 60) -> list[mt.Tool]:
        """The browser's tool definitions, from a browser of no one's (no profile)."""
        probe = self._session(self._address("probe"))
        probe.start()
        try:
            await asyncio.wait_for(probe.ready.wait(), timeout=wait)
            return probe.tools
        finally:
            await probe.stop()
            with contextlib.suppress(Exception):
                await self._supervise("/stop", "probe")

    def start(self, every: float = 60) -> None:
        self._reaper = asyncio.create_task(self._reap(every))

    def session(self, account_id: str) -> BrowserSession:
        s = self._sessions.get(account_id)
        if s is None:
            s = self._sessions[account_id] = self._session(self._address(account_id))
            s.start()
            log.info("browser connected for an account (%d open)", len(self._sessions))
        return s

    def for_account(self, account_id: str) -> AccountBrowser:
        return AccountBrowser(self, account_id)

    @contextmanager
    def hold(self, account_id: str):
        """While an account's task runs, its browser isn't closed for being idle."""
        self._held[account_id] = self._held.get(account_id, 0) + 1
        try:
            yield
        finally:
            self._held[account_id] -= 1
            if not self._held[account_id]:
                del self._held[account_id]
            if s := self._sessions.get(account_id):
                s.last_used = time.monotonic()

    def is_open(self, account_id: str) -> bool:
        return account_id in self._sessions

    def touch(self, account_id: str) -> None:
        """The person wrote: their open window counts as used from now (it closes idle_minutes after this)."""
        if s := self._sessions.get(account_id):
            s.last_used = time.monotonic()

    def open_session(self, account_id: str) -> BrowserSession | None:
        s = self._sessions.get(account_id)
        return s if s is not None and s.ready.is_set() else None

    async def close(self, account_id: str, browser_too: bool = False) -> None:
        """Close the connection; `browser_too`: the person's browser as well (its profile stays on disk)."""
        s = self._sessions.pop(account_id, None)
        if s is not None:
            await s.stop()
            log.info("browser disconnected for an account (%d open)", len(self._sessions))
        if browser_too:
            with contextlib.suppress(Exception):
                await self._supervise("/stop", account_id)

    async def forget(self, account_id: str) -> None:
        """Clear browser: the person's browser stops and their whole profile folder is deleted."""
        await self.close(account_id)
        await self._supervise("/forget", account_id)

    async def reap(self) -> None:
        if self.idle_seconds <= 0:  # the default: windows stay open, like the person's own browser (SPEC §3.4)
            return
        now = time.monotonic()
        for account_id, s in list(self._sessions.items()):
            if account_id not in self._held and not s.busy and now - s.last_used >= self.idle_seconds:
                await self.close(account_id, browser_too=True)

    async def _reap(self, every: float) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await self.reap()
            except Exception:
                log.exception("closing idle browser contexts failed")

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for account_id in list(self._sessions):
            await self.close(account_id)


class AccountBrowser:
    """An account's way into the browser, as tools see it (`ctx.browser`): opens its context on first use."""

    def __init__(self, pool: BrowserPool, account_id: str) -> None:
        self._pool = pool
        self._account_id = account_id

    async def call(self, name: str, args: dict, timeout: float = 90) -> mt.CallToolResult:
        return await self._pool.session(self._account_id).call(name, args, timeout)

    def is_open(self) -> bool:
        return self._pool.is_open(self._account_id)


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
    full = text
    if len(text) > MAX_SNAPSHOT_CHARS:
        text = text[:MAX_SNAPSHOT_CHARS] + "\n[… snapshot cut to fit; use browser_find to look for something specific]"
    return ToolResult(text, images, kind="page", error=bool(r.isError), full=full)


def http_status(text: str) -> int | None:
    """The page's HTTP status as Playwright MCP reports it, if it did."""
    m = HTTP_STATUS.search(text)
    return int(m.group(1)) if m else None


def tool_hash(t: mt.Tool) -> str:
    return hashlib.sha256(json.dumps({"d": t.description, "s": t.inputSchema}, sort_keys=True).encode()).hexdigest()


def pinned(tools: list[mt.Tool], path: Path) -> list[mt.Tool]:
    """First start: record each tool definition. Later: hide tools whose definition changed (SPEC §5.2)."""
    current = {t.name: tool_hash(t) for t in tools}
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=1))
        return tools
    known = json.loads(path.read_text())
    ok = [t for t in tools if known.get(t.name) == current[t.name]]
    for t in tools:
        if known.get(t.name) != current[t.name]:
            log.warning("browser tool %s is new or changed since pinning; hidden (aha doctor --repin)", t.name)
    return ok


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
REF_TARGET = re.compile(r"^\[?\s*(?:ref\s*[=:]\s*)?((?:f\d+)?e\d+)\s*\]?$")
# A field only the sign-in card may fill: its page label says password or code (SPEC §3.4 "Signing in").
SECRET_FIELD = re.compile(r"pass ?word|passcode|\bpin\b|one[- ]time|verification code|security code|\botp\b|2fa", re.I)
YAML_BLOCK = re.compile(r"```yaml\n(.*?)(?:```|\Z)", re.S)  # tolerant of a snapshot cut to fit


def normalise_targets(args: dict) -> dict:
    """The model often writes "[ref=e23]" where the browser wants "e23"."""

    def fix(v):
        m = REF_TARGET.match(v.strip()) if isinstance(v, str) else None
        return m.group(1) if m else v

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
        f'[Aha!: a cookie banner was over this page; it was closed with "{label}". This is the page now.]\n\n'
        + head
        + "### Page"
        + snap.text.split("### Page", 1)[1]
    )


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
    note_blank(ctx, result)
    if m := YAML_BLOCK.search(result.text):
        ctx.task.last_snapshot = m.group(1)
        note_sign_in_form(ctx, result, m.group(1))
        note_challenge(ctx, result, m.group(1))
    note_page(ctx, result.text)


def note_blank(ctx: ToolContext, result: ToolResult) -> None:
    """A new window's blank page: nothing was lost, open the site again (SPEC §3.4 "A new window starts blank")."""
    m = PAGE_URL.search(result.text)
    if not m:
        return
    ctx.task.blank_page = m.group(1) == "about:blank"
    if not ctx.task.blank_page:
        return
    last = signins.last_page(ctx.account) if ctx.account is not None else ""
    where = f" The last page open was {last}." if last else ""
    result.text = (
        "[Aha!: this is a new browser window: the browser closes when it isn't used for a while and opens again "
        "blank. It still has the user's cookies and sign-ins, so nothing was lost and no session ended because of "
        f"it. Open the site again with browser_navigate and carry on.{where}]\n\n" + result.text
    )


def note_challenge(ctx: ToolContext, result: ToolResult, snapshot: str) -> None:
    """An "are you human?" check on the page: say so, and that the person does it on a card (SPEC §3.4)."""
    m = PAGE_URL.search(result.text)
    host = urlsplit(m.group(1)).hostname or "" if m else ""
    if not host or not CHALLENGE.search(snapshot):
        ctx.task.challenge_page = ""
        return
    ctx.task.challenge_page = site_of(host)
    # Challenge pages often answer 403 or 429: that's the check, not a page that won't load.
    result.text = re.sub(r"^The site answered HTTP \d{3}\. " + re.escape(CANT_TELL) + "\n", "", result.text)
    result.error = False
    result.text = (
        '[Aha!: this page shows an "are you human?" check. If one click on an "I\'m not a robot" box doesn\'t '
        "pass it (or there's no box), call browser_human_check now: the user does it themselves. Don't just tell "
        "them about it, and never solve a picture puzzle yourself.]\n\n" + result.text
    )


def is_sign_in_form(snapshot: str, url: str) -> bool:
    if PASSWORD_FIELD.search(snapshot):
        return True
    return bool(USER_FIELD.search(snapshot)) and bool(
        SIGN_IN_CONTROL.search(snapshot) or SIGN_IN_ADDRESS.search(urlsplit(url).path)
    )


def note_sign_in_form(ctx: ToolContext, result: ToolResult, snapshot: str) -> None:
    """Say in code, not leave to the model, that the page asks the person to sign in (SPEC §3.4 "Signing in").

    A session the browser remembered can have ended: the page is what counts, not an earlier sign-in."""
    m = PAGE_URL.search(result.text)
    url = m.group(1) if m else ""
    host = urlsplit(url).hostname or ""
    if not host or not is_sign_in_form(snapshot, url):
        ctx.task.sign_in_page = ""
        return
    site = site_of(host)
    ctx.task.sign_in_page = site
    if ctx.task.signed_in_sites is None:
        ctx.task.signed_in_sites = signins.signed_in_sites(ctx.account)
    before = (
        f"They signed in on {site} before, but that session has ended: they are NOT signed in there now. "
        if site in ctx.task.signed_in_sites
        else ""
    )
    result.text = (
        f"[Aha!: this page is a sign-in form on {site}. {before}If the task needs their account, call "
        "browser_login now; don't tell them they're signed in.]\n\n" + result.text
    )


def note_page(ctx: ToolContext, text: str) -> None:
    """SPEC §5.6: has this task had a page of a site the person signed in on? From the browser's own address."""
    if ctx.task.private or not (m := PAGE_URL.search(text)):
        return
    host = urlsplit(m.group(1)).hostname or ""
    if not host:
        return
    if ctx.task.signed_in_sites is None:
        ctx.task.signed_in_sites = signins.signed_in_sites(ctx.account)
    if site_of(host) in ctx.task.signed_in_sites or site_of(host) in MESSAGE_SITES:
        ctx.task.private = True  # the person's account pages, or their messages


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


async def run_approved(session, name: str, args: dict, ctx: ToolContext) -> ToolResult:
    """Run an approved action; typing goes at human pace where it can (SPEC §3.4)."""
    typing = name == "browser_type" and not args.get("slowly") and isinstance(args.get("text"), str)
    # On a messaging site Enter sends: a new line in the text is Shift+Enter there (SPEC §3.4).
    host = urlsplit(ctx.task.page_url).hostname or ""
    newline = "Shift+Enter" if host and site_of(host) in MESSAGE_SITES else "Enter"
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
                if name == "browser_click" and human.is_ref(args.get("target")):
                    await run_code(session, human.move_code(args["target"]))  # the mouse gets there first (§3.4)
                return await call_with_snapshot(session, name, args, ctx)
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
    result = await run_approved(session, name, args, ctx)
    if action.kind in ("open", "search"):
        if not result.error:
            guard.opened(ctx, str(args.get("url", "")))
        return result
    if what := entered(name, args, ctx):
        result.text = what + "\n\n" + result.text
    return result


def entered(name: str, args: dict, ctx: ToolContext) -> str:
    """What was typed or chosen, as plain fact (the person may have changed it on a card; the model can't tell)."""
    spec = TYPING.get(name)
    return spec.entered(args, labels_of(name, args, ctx)) if spec else ""


async def screenshot(session) -> Image | None:
    try:
        r = to_result(await session.call("browser_take_screenshot", {"type": "jpeg"}))
    except Exception:
        log.warning("couldn't take a screenshot")
        return None
    return r.images[0] if r.images else None


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
LOGIN_BOX = re.compile(r'^\s*- textbox "([^"]*)"[^\n]*?\[ref=((?:f\d+)?e\d+)\]', re.M)
CODE_LABEL = re.compile(r"one[- ]time|verification|security code|\bcode\b|\botp\b|2fa", re.I)
PASSWORD_LABEL = re.compile(r"pass ?word|passcode|passwort|mot de passe|senha|contrase|\bpin\b", re.I)
USER_LABEL = re.compile(r"e-?mail|user ?name|user id|log ?in|sign ?in|phone|mobile|account", re.I)
MAIL_NOTE = (
    "This came up after Aha! read your email, calendar or an account page: sign in only if this is the site you meant."
)
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
            if ctx.task.private:
                card.append(Field("Check", MAIL_NOTE, "info"))
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
    if signed:
        try:  # they stay signed in, like "remember me" in their own browser (SPEC §3.4)
            signins.signed_in(ctx.account, host, site)  # the browser keeps the session in its profile
        except Exception:
            log.exception("couldn't note a sign-in")
        if ctx.task.signed_in_sites is not None:
            ctx.task.signed_in_sites.add(site)
        ctx.task.private = True  # the task now holds private data (SPEC §5.2)
    ctx.task.last_snapshot = signins.redact(ctx.task.last_snapshot, ctx.task.secrets)
    said = f"Signed in on {host}." if signed else f"Not signed in on {host}: look at the page for why."
    page.text = said + " The page now:\n\n" + signins.redact(page.text, ctx.task.secrets)
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


async def register(reg: Registry, pool: BrowserPool, pin_file: Path, wait: float = 60) -> int:
    try:
        tools = await pool.tools(wait)
    except TimeoutError:
        log.warning("browser not reachable; browsing is unavailable until restart")
        return 0
    count = 0
    for t in pinned(tools, pin_file):
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
