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
from aha_local.tools.browser_pool import BrowserPool, BrowserSession

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

        self.calls: list[tuple[str, str]] = []

        async def supervise(path: str, account_id: str) -> dict:
            self.calls.append((path, account_id))
            return {"port": 8931}

        self.supervise = supervise

    def open(self) -> list[FakeContext]:
        return [c for c in self.contexts if not c.closed]


@pytest.fixture
def fake_browser() -> FakeBrowser:
    return FakeBrowser()


@pytest.fixture
async def pool(fake_browser):
    p = BrowserPool("http://aha-browser:8930", session=fake_browser.Session, supervise=fake_browser.supervise)
    yield p
    await p.stop()


async def browsing_agent(config, accounts, llm, registry, chat, pool, tmp_path: Path) -> Agent:
    assert await browser.register(registry, pool, wait=2) == 3
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


async def test_deleting_an_account_discards_its_browser_context(pool, fake_browser, accounts):
    from types import SimpleNamespace

    from aha_local.service import Service

    fe = person(accounts)
    await pool.for_account(fe.id).call("browser_navigate", {"url": "https://a.example/"})
    stopped = []

    async def stop(account, quiet=False):
        stopped.append(account.name)

    agent = SimpleNamespace(stop=stop, sessions={fe.address: object()})
    service = SimpleNamespace(
        accounts=accounts,
        agent=agent,
        browser=pool,
        channel=SimpleNamespace(revoke=None),
        vault=accounts.vault,
    )
    assert (await Service._handle(service, {"cmd": "delete", "account": fe.name}))["ok"]
    assert not pool.is_open(fe.id) and not fake_browser.open()
    assert ("/forget", fe.id) in fake_browser.calls  # their whole browser profile is deleted
    assert stopped == [fe.name] and not agent.sessions  # its task stopped, nothing of it left in memory


def test_each_person_has_a_browser_and_profile_of_their_own():
    """SPEC §3.4 "Browser profiles": one Playwright MCP per person, its own profile folder in the encrypted mount;
    the probe for the tool definitions has none."""
    deploy = Path(__file__).parents[1] / "deploy"
    supervisor = (deploy / "browser" / "supervisor.mjs").read_text()
    assert '"--user-data-dir", `${PROFILES}/${id}`' in supervisor and "--shared-browser-context" not in supervisor
    assert 'id === "probe" ? ["--isolated"]' in supervisor
    assert "/^(?:[a-f0-9]{8,64}|probe)$/" in supervisor  # an id is never a path
    unit = (deploy / "quadlet" / "aha-browser.container").read_text()
    assert "Volume=@PROFILES_ENC@:/profiles.enc" in unit and "LoadCredentialEncrypted=profiles-key:" in unit
    start = (deploy / "browser" / "start.sh").read_text()
    # mounted inside the container: the decrypted profiles never exist on the host
    assert "gocryptfs -q -noprealloc -passfile" in start and "/profiles.enc /profiles" in start
