# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""One browser context per account (SPEC §3.4): no tabs, cookies or pages cross between people."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import mcp.types as mt
import pytest

from aha_local.agent import Agent
from aha_local.tools import browser
from aha_local.tools.browser import BrowserPool, BrowserSession

from .conftest import person, settle

TOOLS = [
    mt.Tool(name=n, description=n, inputSchema={"type": "object", "properties": {}})
    for n in ("browser_navigate", "browser_tabs", "browser_snapshot")
]


class FakeContext:
    """What one MCP connection to Playwright MCP gets: its own tabs and cookies."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.cookies: dict[str, str] = {}
        self.closed = False

    async def call_tool(self, name: str, args: dict) -> mt.CallToolResult:
        if name == "browser_navigate":
            self.url = args["url"]
            self.cookies[self.url] = "session=logged-in"
        text = f"### Open tabs\n- 0: (current) [Page]({self.url})\n### Cookies\n{json.dumps(self.cookies)}"
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)], isError=False)


class FakeBrowser:
    """Stands in for aha-browser: hands each new connection a fresh context, like Playwright MCP does."""

    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        browser_server = self

        class Session(BrowserSession):
            async def _run(self) -> None:
                ctx = FakeContext()
                browser_server.contexts.append(ctx)
                self.tools = TOOLS
                self.ready.set()
                try:
                    while (item := await self.queue.get()) is not None:
                        fn, fut = item
                        fut.set_result(await fn(ctx))
                finally:
                    ctx.closed = True

        self.Session = Session

    def open(self) -> list[FakeContext]:
        return [c for c in self.contexts if not c.closed]


@pytest.fixture
def fake_browser() -> FakeBrowser:
    return FakeBrowser()


@pytest.fixture
async def pool(fake_browser):
    p = BrowserPool("http://aha-browser:8931/mcp", idle_seconds=1800, session=fake_browser.Session)
    yield p
    await p.stop()


async def browsing_agent(config, accounts, llm, registry, chat, pool, tmp_path: Path) -> Agent:
    assert await browser.register(registry, pool, tmp_path / "pins.json", wait=2) == 3
    return Agent(config, accounts, llm, registry, chat, pool)


def tool_results(llm) -> list[str]:
    return [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]


async def test_two_people_never_see_each_others_tabs_or_cookies(
    config, accounts, llm, registry, chat, pool, fake_browser, tmp_path
):
    agent = await browsing_agent(config, accounts, llm, registry, chat, pool, tmp_path)
    fe, hb = person(accounts), accounts.create("Hb")
    assert not fake_browser.open()  # the startup probe's connection is gone, and nobody browsed yet

    llm.call("browser_navigate", url="https://bank.example/account")
    llm.answer("Done.")
    await agent.inbound(fe, "open my bank")
    await settle(agent)

    llm.call("browser_tabs", action="list")
    llm.answer("You have no tabs open.")
    await agent.inbound(hb, "what tabs do I have open?")
    await settle(agent)

    seen_by_hb = "\n".join(tool_results(llm))
    assert "about:blank" in seen_by_hb
    assert "bank.example" not in seen_by_hb and "logged-in" not in seen_by_hb
    assert len(fake_browser.open()) == 2 and pool.is_open(fe.id) and pool.is_open(hb.id)

    # Fe's page is still there for Fe's follow-up.
    llm.call("browser_tabs", action="list")
    llm.answer("Your bank is open.")
    await agent.inbound(fe, "is it still open?")
    await settle(agent)
    assert "bank.example" in "\n".join(tool_results(llm))


async def test_two_people_browse_at_the_same_time_on_their_own_pages(pool, fake_browser):
    fe, hb = pool.for_account("fe"), pool.for_account("hb")
    await asyncio.gather(
        fe.call("browser_navigate", {"url": "https://a.example/"}),
        hb.call("browser_navigate", {"url": "https://b.example/"}),
    )
    fe_tabs = (await fe.call("browser_tabs", {"action": "list"})).content[0].text
    hb_tabs = (await hb.call("browser_tabs", {"action": "list"})).content[0].text
    assert "a.example" in fe_tabs and "b.example" not in fe_tabs
    assert "b.example" in hb_tabs and "a.example" not in hb_tabs


async def test_an_idle_context_is_discarded_and_the_next_one_starts_empty(fake_browser):
    pool = BrowserPool("x", idle_seconds=0, session=fake_browser.Session)
    fe = pool.for_account("fe")
    await fe.call("browser_navigate", {"url": "https://shop.example/basket"})
    with pool.hold("fe"):  # while a task runs, it isn't closed
        await pool.reap()
        assert pool.is_open("fe")
    await pool.reap()
    assert not pool.is_open("fe") and not fake_browser.open()
    tabs = (await fe.call("browser_tabs", {"action": "list"})).content[0].text
    assert "about:blank" in tabs and "shop.example" not in tabs
    await pool.stop()
    assert not fake_browser.open()


async def test_a_context_in_use_is_not_closed_for_idleness(fake_browser):
    pool = BrowserPool("x", idle_seconds=0, session=fake_browser.Session)
    s = pool.session("fe")
    await asyncio.wait_for(s.ready.wait(), 1)
    s.busy = 1
    await pool.reap()
    assert pool.is_open("fe")
    s.busy = 0
    await pool.reap()
    assert not pool.is_open("fe")


async def test_deleting_an_account_discards_its_browser_context(pool, fake_browser, accounts):
    from types import SimpleNamespace

    from aha_local.service import Service

    fe = person(accounts)
    await pool.for_account(fe.id).call("browser_navigate", {"url": "https://a.example/"})
    service = SimpleNamespace(
        accounts=accounts,
        browser=pool,
        channel=SimpleNamespace(revoke=None),
        vault=accounts.vault,
    )
    assert (await Service._handle(service, {"cmd": "delete", "account": fe.name}))["ok"]
    assert not pool.is_open(fe.id) and not fake_browser.open()


def test_the_browser_container_never_shares_a_context_between_connections():
    unit = (Path(__file__).parents[1] / "deploy" / "quadlet" / "aha-browser.container").read_text()
    assert "--isolated" in unit and "--shared-browser-context" not in unit
