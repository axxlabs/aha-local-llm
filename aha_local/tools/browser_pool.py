# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The browser connections (SPEC §3.4 "Browser profiles"): one Playwright MCP per person, started by
aha-browser's supervisor, reached only through that person's own `AccountBrowser`."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import mcp.types as mt
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger("aha.browser")


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
