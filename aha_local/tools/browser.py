# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The browser: Playwright MCP behind the policy (SPEC §3.4, §5.2).

One MCP connection to aha-browser per account, so one browser context per account: tabs, cookies, storage and
logins never cross between people (SPEC §3.4). A task only ever reaches its own account's connection, through
`ctx.browser`; the model can't name another. A connection opens on the account's first browser call, keeps
its page between tasks, and closes after `idle_minutes` unused, on `aha delete`, and when the agent stops.

Each browser tool the model sees is wrapped: the policy decides auto, approval or block from the real
arguments; approvals are answered only by the person (approvals.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import mcp.types as mt
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..approvals import Field
from ..items import Image
from ..policy.browser import decide, form_for, offered
from ..policy.urls import check_url, site_of
from . import signins
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

log = logging.getLogger("aha.browser")

MAX_SNAPSHOT_CHARS = 30_000
HTTP_STATUS = re.compile(r"^- HTTP status: (\d{3})", re.M)
CANT_TELL = (
    "From here you can't tell why: tell the person you couldn't load the page and give the link; "
    "don't guess at a cause (region, IP, outage) the page didn't state."
)
APPROVED_NOTE = (
    "The user approved this action; that approval is their go-ahead. "
    "Continue the task without asking for confirmation in the chat."
)


class BrowserSession:
    """One MCP connection, owned by a background task so its page survives between calls."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.tools: list[mt.Tool] = []
        self.busy = 0
        self.last_used = time.monotonic()
        self.opening: asyncio.Task | None = None  # restoring kept sign-ins: calls wait for it
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self, timeout: float = 5) -> None:
        """Close the connection, which discards its browser context."""
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
                async with streamablehttp_client(self.url) as (r, w, _), ClientSession(r, w) as s:
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
    """One BrowserSession per account (SPEC §3.4)."""

    def __init__(self, url: str, idle_seconds: float, session=BrowserSession) -> None:
        self.url = url
        self.idle_seconds = idle_seconds
        self._session = session
        self._sessions: dict[str, BrowserSession] = {}
        self._held: dict[str, int] = {}
        self._reaper: asyncio.Task | None = None
        # Called once when an account's context opens, before its first call: restores kept sign-ins (SPEC §3.4).
        self.on_open = None

    async def tools(self, wait: float = 60) -> list[mt.Tool]:
        """The browser's tool definitions, from a short-lived connection of no account's."""
        probe = self._session(self.url)
        probe.start()
        try:
            await asyncio.wait_for(probe.ready.wait(), timeout=wait)
            return probe.tools
        finally:
            await probe.stop()

    def start(self, every: float = 60) -> None:
        self._reaper = asyncio.create_task(self._reap(every))

    def session(self, account_id: str) -> BrowserSession:
        s = self._sessions.get(account_id)
        if s is None:
            s = self._sessions[account_id] = self._session(self.url)
            s.start()
            if self.on_open is not None:
                s.opening = asyncio.create_task(self._open(account_id, s))
            log.info("browser context opened for an account (%d open)", len(self._sessions))
        return s

    async def _open(self, account_id: str, s: BrowserSession) -> None:
        try:
            await self.on_open(account_id, s)
        except Exception:
            log.exception("preparing a browser context failed")

    def for_account(self, account_id: str) -> AccountBrowser:
        return AccountBrowser(self, account_id)

    @contextmanager
    def hold(self, account_id: str):
        """While an account's task runs, its context isn't closed for being idle."""
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

    async def close(self, account_id: str) -> None:
        s = self._sessions.pop(account_id, None)
        if s is not None:
            await s.stop()
            log.info("browser context closed for an account (%d open)", len(self._sessions))

    async def reap(self) -> None:
        now = time.monotonic()
        for account_id, s in list(self._sessions.items()):
            if account_id not in self._held and not s.busy and now - s.last_used >= self.idle_seconds:
                await self.close(account_id)

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
        s = self._pool.session(self._account_id)
        if s.opening is not None:
            await s.opening
        return await s.call(name, args, timeout)

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
    if len(text) > MAX_SNAPSHOT_CHARS:
        text = text[:MAX_SNAPSHOT_CHARS] + "\n[… snapshot cut to fit; use browser_find to look for something specific]"
    return ToolResult(text, images, kind="page", error=bool(r.isError))


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
REF_TARGET = re.compile(r"^\[?ref=(e\d+)\]?$")
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
            result.text = SNAPSHOT_LINK.sub(
                "### Snapshot\n" + snap.text.split("### Snapshot", 1)[-1].strip(), result.text
            )
    if m := YAML_BLOCK.search(result.text):
        ctx.task.last_snapshot = m.group(1)
    return result


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
        if ctx.task.read_mail:  # SPEC §5.5: after reading mail, every address the model picks is a card
            verdict = decide(name, args, False, set())
        else:
            verdict = decide(name, args, ctx.config.allow_all_sites, ctx.task.approved_sites)
        if verdict.outcome == "block":
            return ToolResult(f"Blocked by aha's safety policy: {verdict.reason}.", error=True)
        if verdict.outcome == "auto":
            result = await call_with_snapshot(session, name, args, ctx)
            if verdict.site and not result.error:
                ctx.task.approved_sites.add(verdict.site)
            return result

        site = ""
        try:
            site = await current_site(session)
        except Exception:
            log.warning("couldn't read the current site")
        shot = None
        try:
            shot_result = to_result(await session.call("browser_take_screenshot", {}))
            shot = shot_result.images[0] if shot_result.images else None
        except Exception:
            log.warning("couldn't take a screenshot for the approval")
        form = form_for(name, labelled(name, args, ctx), site)
        decision = await ctx.approvals.ask(ctx.account, "action", form.title, form.fields, shot)
        if not decision.approved:
            return declined(decision)
        final = form.apply(name, args, decision.values)
        if name in ("browser_navigate", "browser_tabs"):
            new_site, why = check_url(str(final.get("url", "")))
            if why:
                return ToolResult(f"Blocked by aha's safety policy: {why}.", error=True)
            ctx.task.approved_sites.add(new_site)
        result = await call_with_snapshot(session, name, final, ctx)
        notes = [APPROVED_NOTE]
        if final != args:
            notes.append(
                f"The user edited the values before it ran; use theirs from now on: {describe(form, decision)}"
            )
        result.text = " ".join(notes) + "\n\n" + result.text
        return result

    return handler


def describe(form, decision) -> str:
    return "; ".join(f"{f.label}: {v}" for f, v in zip(form.fields, decision.values, strict=False))


def declined(decision) -> ToolResult:
    if decision.reason == "expired":
        return ToolResult("", error=True, kind="expired")
    text = "The user declined this action, so it was not run. Don't retry it unchanged."
    if decision.note:
        text += f" They said: “{decision.note}”. Follow that."
    else:
        text += " Ask them how to proceed."
    return ToolResult(text, error=True)


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


SIGN_IN_FIELDS = (
    ("username_target", "text", "Email or username"),
    ("password_target", "secret", "Password"),
    ("code_target", "secret", "Code"),
)
KEEP_LABEL = "Keep me signed in on this computer"
MAIL_NOTE = "This came up after Aha! read your email: sign in only if this is the site you meant."


def _ref(value) -> str:
    v = str(value or "").strip()
    m = REF_TARGET.match(v)
    return m.group(1) if m else v


async def browser_sign_in(args: dict, ctx: ToolContext) -> ToolResult:
    """The person signs in on the current page: they type on a card, the harness types into the page (SPEC §3.4)."""
    session = ctx.browser
    if session is None:
        return ToolResult("The browser isn't available right now.", error=True)
    targets = [(ref, kind, default) for key, kind, default in SIGN_IN_FIELDS if (ref := _ref(args.get(key)))]
    if not targets:
        return ToolResult("Give the references of the sign-in fields from the latest snapshot.", error=True)
    url = await current_url(session)
    site, why = check_url(url)
    if why or not site:
        return ToolResult(f"Blocked by aha's safety policy: {why or 'no page is open'}.", error=True)
    if urlsplit(url).scheme != "https":
        return ToolResult("Signing in is only offered on secure (https) pages. Tell the person.", error=True)
    labels = []
    for ref, _, _ in targets:
        label = element_label(ctx.task.last_snapshot, ref)
        if not label:
            return ToolResult(
                f"{ref} isn't in the latest snapshot. Take a snapshot and use the sign-in fields' references.",
                error=True,
            )
        labels.append(label)
    fields = [Field("Site", site, "info")]
    if ctx.task.read_mail:
        fields.append(Field("Check", MAIL_NOTE, "info"))
    first = len(fields)
    for (_, kind, default), label in zip(targets, labels, strict=True):
        fields.append(Field(label or default, "", kind))
    has_password = any(kind == "secret" and default == "Password" for _, kind, default in targets)
    if has_password:
        fields.append(Field(KEEP_LABEL, "false", "bool"))
    decision = await ctx.approvals.ask(ctx.account, "signin", f"Sign in to {site}?", fields, await screenshot(session))
    if not decision.approved:
        if decision.reason == "expired":
            return ToolResult("", error=True, kind="expired")
        text = "The person didn't sign in. Don't ask for their password or code in the chat."
        if decision.note:
            text += f" They said: “{decision.note}”. Follow that."
        return ToolResult(text, error=True)
    values = decision.values[first : first + len(targets)]
    ctx.task.secrets.extend(v for (_, kind, _), v in zip(targets, values, strict=True) if kind == "secret" and v)
    filled = [(t, v) for t, v in zip(targets, values, strict=True) if v]
    if not filled:
        return ToolResult(
            "The person approved but typed nothing, so nothing was entered. Ask what they want.", error=True
        )
    for i, ((ref, _, _), value) in enumerate(filled):
        submit = bool(args.get("submit", True)) and i == len(filled) - 1
        r = await session.call(
            "browser_type",
            {"element": labels[targets.index(filled[i][0])], "target": ref, "text": value, "submit": submit},
        )
        if r.isError:  # the result echoes the typed text: never pass it on
            return ToolResult("Typing into the sign-in form failed. Take a snapshot and try again.", error=True)
    with contextlib.suppress(Exception):
        await session.call("browser_wait_for", {"time": 2})
    kept = has_password and decision.values[-1] == "true"
    if kept:
        try:
            await signins.keep(ctx.account, session, site)
            ctx.task.signed_in.add(site)
        except Exception:
            log.exception("couldn't keep a sign-in")
            kept = False
    page = await call_with_snapshot(session, "browser_snapshot", {}, ctx)
    page.text = (
        f"The person signed in on {site} themselves; you never see what they typed. "
        + ("They chose to stay signed in on this computer. " if kept else "")
        + "If the page asks for a one-time code, call browser_sign_in again with the code field. The page now:\n\n"
        + signins.redact(page.text, ctx.task.secrets)
    )
    ctx.task.last_snapshot = signins.redact(ctx.task.last_snapshot, ctx.task.secrets)
    return page


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
        "browser_sign_in",
        "When the page asks the user to sign in (or for a one-time code), let them do it: give the references of "
        "the fields from the latest snapshot. The user types their details on a sign-in card; they're entered "
        "for them and you never see them. Never ask for passwords or codes in the chat.",
        params(
            username_target=string("Reference of the email or username field, if the page has one"),
            password_target=string("Reference of the password field, if the page has one"),
            code_target=string("Reference of the one-time code field, if the page has one"),
            submit={"type": "boolean", "description": "Press Enter after the last field (default true)"},
        ),
        browser_sign_in,
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
        reg.add(Tool(t.name, t.description or "", t.inputSchema, make_handler(t.name)))
        count += 1
    if count:
        for tool in HARNESS_TOOLS:
            reg.add(tool)
    log.info("browser: %d tools, one context per account", count)
    return count
