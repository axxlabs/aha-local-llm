# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The browser: Playwright MCP behind the policy (SPEC §3.4, §5.2).

One persistent MCP session to aha-browser, owned by a background task, so the page survives between calls
and tasks. Each browser tool the model sees is wrapped: the policy decides auto, approval or block from the
real arguments; approvals are answered only by the person (approvals.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import mcp.types as mt
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..items import Image
from ..policy.browser import decide, form_for, offered
from ..policy.urls import check_url, site_of
from .registry import Registry, Tool, ToolContext, ToolResult

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
    def __init__(self, url: str) -> None:
        self.url = url
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.tools: list[mt.Tool] = []
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                async with streamablehttp_client(self.url) as (r, w, _), ClientSession(r, w) as s:
                    await s.initialize()
                    self.tools = (await s.list_tools()).tools
                    self.ready.set()
                    log.info("browser connected: %d tools", len(self.tools))
                    while True:
                        fn, fut = await self.queue.get()
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
        await asyncio.wait_for(self.ready.wait(), timeout=30)
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put((lambda s: s.call_tool(name, args), fut))
        return await asyncio.wait_for(fut, timeout)


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


async def current_site(session: BrowserSession) -> str:
    """The site of the page the browser is on, read from the browser itself (not from the model)."""
    r = await session.call("browser_tabs", {"action": "list"})
    for c in r.content:
        if isinstance(c, mt.TextContent):
            for line in c.text.splitlines():
                if "(current)" in line and "http" in line:
                    url = line[line.index("http") :].split()[0].strip("()[]")
                    host = urlsplit(url).hostname or ""
                    return site_of(host) if host else ""
    return ""


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


async def call_with_snapshot(session: BrowserSession, name: str, args: dict, ctx: ToolContext) -> ToolResult:
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


def make_handler(session: BrowserSession, name: str):
    async def handler(args: dict, ctx: ToolContext) -> ToolResult:
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


async def register(reg: Registry, session: BrowserSession, pin_file: Path, wait: float = 60) -> int:
    try:
        await asyncio.wait_for(session.ready.wait(), timeout=wait)
    except TimeoutError:
        log.warning("browser not reachable; browsing is unavailable until restart")
        return 0
    count = 0
    for t in pinned(session.tools, pin_file):
        if not offered(t.name):
            continue
        reg.add(Tool(t.name, t.description or "", t.inputSchema, make_handler(session, t.name)))
        count += 1
    return count
