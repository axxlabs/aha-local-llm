# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Signing in to websites and showing screenshots (SPEC §3.4 "Signing in", tests 17 and 18).

A fake Playwright MCP stands in for aha-browser: each connection its own context with an address, sign-in
fields that echo what was typed (as real snapshots and "Ran Playwright code" results do), and cookies.
"""

from __future__ import annotations

import base64
import json
import re

import mcp.types as mt
import pytest

from aha_local.agent import Agent
from aha_local.tools import browser, signins
from aha_local.tools.browser import BrowserPool, BrowserSession

from .conftest import person, settle

PASSWORD = "correct-horse-battery"
CODE = "482915"
PNG = base64.b64encode(b"\x89PNG fake").decode()
TOOLS = [
    mt.Tool(name=n, description=n, inputSchema={"type": "object", "properties": {}})
    for n in (
        "browser_navigate",
        "browser_tabs",
        "browser_snapshot",
        "browser_type",
        "browser_take_screenshot",
        "browser_wait_for",
    )
]


def text_result(text: str, error: bool = False) -> mt.CallToolResult:
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)], isError=error)


class FakeContext:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.fields: dict[str, str] = {}
        self.cookies: list[dict] = []
        self.calls: list[str] = []

    def snapshot(self) -> str:
        return (
            f"### Page\n- Page URL: {self.url}\n### Snapshot\n```yaml\n"
            f'- textbox "Email address" [ref=e1]: {self.fields.get("e1", "")}\n'
            f'- textbox "Password" [ref=e2]: {self.fields.get("e2", "")}\n'
            f'- textbox "Verification code" [ref=e3]: {self.fields.get("e3", "")}\n'
            '- button "Sign in" [ref=e4]\n```'
        )

    async def call_tool(self, name: str, args: dict) -> mt.CallToolResult:
        self.calls.append(name)
        if name == "browser_navigate":
            self.url = args["url"]
            return text_result(self.snapshot())
        if name == "browser_tabs":
            return text_result(f"### Result\n- 0: (current) [Sign in]({self.url})")
        if name == "browser_snapshot":
            return text_result(self.snapshot())
        if name == "browser_type":
            self.fields[args["target"]] = args["text"]
            if args.get("submit"):
                self.cookies.append(
                    {"name": "sid", "value": "s3ss10n", "domain": ".waitrose.com", "path": "/", "expires": -1}
                )
            return text_result(f"### Ran Playwright code\n```js\nawait page.fill('{args['text']}');\n```")
        if name == "browser_take_screenshot":
            return mt.CallToolResult(content=[mt.ImageContent(type="image", data=PNG, mimeType="image/jpeg")])
        if name == "browser_wait_for":
            return text_result("waited")
        if name == "browser_run_code_unsafe":
            code = args["code"]
            if code == signins.GET_COOKIES:
                return text_result("### Result\n" + json.dumps(json.dumps(self.cookies)))
            literal = json.loads(re.search(r'JSON\.parse\(("(?:[^"\\]|\\.)*")\)', code).group(1))
            if "clearCookies" in code:
                self.cookies = []
            self.cookies.extend(json.loads(literal))
            return text_result('### Result\n"ok"')
        return text_result(f"unknown tool {name}", error=True)


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        server = self

        class Session(BrowserSession):
            async def _run(self) -> None:
                ctx = FakeContext()
                server.contexts.append(ctx)
                self.tools = TOOLS
                self.ready.set()
                while (item := await self.queue.get()) is not None:
                    fn, fut = item
                    fut.set_result(await fn(ctx))

        self.Session = Session


@pytest.fixture
async def setup(config, accounts, llm, registry, chat, tmp_path):
    fake = FakeBrowser()
    pool = BrowserPool("x", idle_seconds=1800, session=fake.Session)

    async def opened(account_id, session):
        await signins.restore(accounts.get(account_id), session)

    pool.on_open = opened
    assert await browser.register(registry, pool, tmp_path / "pins.json", wait=2) == 6
    agent = Agent(config, accounts, llm, registry, chat, pool)
    yield agent, pool, fake
    await pool.stop()


def everything_recorded(agent, llm, chat, account) -> str:
    """All the places a typed password must never reach."""
    return "\n".join(
        [
            json.dumps(llm.requests, default=str),
            json.dumps(account.read_transcript()),
            b"\n".join(account.vault.records(account.dir / "audit.jsonl")).decode(),
            json.dumps([(m.text, m.card) for m in chat.sent], default=str),
            json.dumps(account.load_context()),
        ]
    )


async def start_sign_in(agent, llm, chat, account, url="https://www.waitrose.com/login"):
    llm.call("browser_navigate", url=url)
    llm.call("browser_sign_in", username_target="e1", password_target="[ref=e2]")
    llm.call("browser_snapshot")  # a later page that shows the field again
    llm.answer("You're signed in.")
    await agent.inbound(account, "sign in to my Waitrose account")
    return await chat.next()


async def test_the_person_types_the_password_and_the_model_never_sees_it(setup, llm, chat, accounts):
    agent, _, fake = setup
    fe = person(accounts)
    card = await start_sign_in(agent, llm, chat, fe)
    c = card.card
    assert c["kind"] == "signin" and c["title"] == "Sign in to waitrose.com?"  # from the address bar
    assert [(f["label"], f["value"], f["kind"]) for f in c["fields"]] == [
        ("Site", "waitrose.com", "info"),
        ("Email address", "", "text"),  # the page's own label
        ("Password", "", "secret"),
        ("Keep me signed in on this computer", "false", "bool"),
    ]
    assert c["buttons"] == ["Sign in", "Don't"] and c.get("image")
    await agent.approval_reply(fe, c["id"], "approve", ["waitrose.com", "fe@example.com", PASSWORD, "false"])
    await settle(agent)
    page = fake.contexts[-1]
    assert page.fields == {"e1": "fe@example.com", "e2": PASSWORD}
    recorded = everything_recorded(agent, llm, chat, fe)
    assert PASSWORD not in recorded
    # The page echoed it back; the model saw it masked.
    assert "••••" in json.dumps(llm.requests[-1]["messages"], ensure_ascii=False)
    assert "signed in on waitrose.com themselves" in recorded
    assert not signins.sites(fe)  # not kept unless chosen


async def test_on_shared_hosting_the_card_names_the_exact_host(setup, llm, chat, accounts):
    agent, _, _ = setup
    fe = person(accounts)
    card = await start_sign_in(agent, llm, chat, fe, url="https://waitrose-login.herokuapp.com/signin")
    assert card.card["title"] == "Sign in to waitrose-login.herokuapp.com?"
    assert card.card["fields"][0]["value"] == "waitrose-login.herokuapp.com"


async def test_a_declined_sign_in_types_nothing(setup, llm, chat, accounts):
    agent, _, fake = setup
    fe = person(accounts)
    card = await start_sign_in(agent, llm, chat, fe)
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)
    assert len(fake.contexts) == 2 and fake.contexts[-1].url.startswith("https://www.waitrose.com")
    assert fake.contexts[-1].fields == {} and "browser_type" not in fake.contexts[-1].calls
    assert "didn't sign in" in json.dumps(llm.requests[-1]["messages"])


async def test_typed_text_never_fills_a_sign_in_card(setup, llm, chat, accounts):
    agent, _, fake = setup
    fe = person(accounts)
    card = await start_sign_in(agent, llm, chat, fe)
    await agent.approval_reply(fe, card.card["id"], "edit")
    assert "card in the app" in (await chat.next()).text
    await agent.inbound(fe, "2: hunter2")  # the text fallback: declines with a note, never types
    await settle(agent)
    assert fake.contexts[-1].fields == {}


async def test_only_secure_pages_and_fields_on_the_page(setup, llm, chat, accounts):
    agent, _, _ = setup
    fe = person(accounts)
    llm.call("browser_navigate", url="http://shop.example.com/login")
    llm.call("browser_sign_in", password_target="e2")
    llm.call("browser_navigate", url="https://shop.example.com/login")
    llm.call("browser_sign_in", password_target="e99")
    llm.answer("Couldn't.")
    await agent.inbound(fe, "sign in")
    await settle(agent)
    results = [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]
    assert "only offered on secure (https) pages" in results[1]
    assert "e99 isn't in the latest snapshot" in results[3]
    assert not any(m.card for m in chat.sent)


async def test_kept_sign_ins_come_back_only_for_the_same_person_and_can_be_forgotten(setup, llm, chat, accounts):
    agent, pool, fake = setup
    fe, hb = person(accounts), accounts.create("Hb")
    card = await start_sign_in(agent, llm, chat, fe)
    await agent.approval_reply(fe, card.card["id"], "approve", ["waitrose.com", "fe@example.com", PASSWORD, "true"])
    await settle(agent)
    assert signins.sites(fe) == ["waitrose.com"]
    raw = (fe.dir / "sign-ins.json").read_bytes()
    assert b"s3ss10n" not in raw and b"waitrose" not in raw  # encrypted at rest

    await pool.close(fe.id)  # idle timeout: the context is gone…
    await pool.for_account(fe.id).call("browser_tabs", {"action": "list"})
    assert [c["value"] for c in fake.contexts[-1].cookies] == ["s3ss10n"]  # …and comes back signed in
    await pool.for_account(hb.id).call("browser_tabs", {"action": "list"})
    assert fake.contexts[-1].cookies == []  # never into another person's browser

    await agent.inbound(fe, "sign-ins")
    assert "waitrose.com" in chat.sent[-1].text
    await agent.inbound(fe, "forget sign-in waitrose.com")
    assert "Signed out of waitrose.com" in chat.sent[-1].text
    assert not signins.sites(fe) and not (fe.dir / "sign-ins.json").exists()
    assert fake.contexts[-2].cookies == []  # signed out of the open context too


async def test_show_screenshot_sends_the_page_to_the_persons_chat(setup, llm, chat, accounts):
    agent, _, _ = setup
    fe = person(accounts)
    llm.call("show_screenshot")  # nothing open yet
    llm.call("browser_navigate", url="https://www.google.com/search?q=fictional+bistro")
    llm.call("show_screenshot", caption="Search results")
    llm.answer("There you go.")
    await agent.inbound(fe, "show me a screenshot of the search")
    await settle(agent)
    results = [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]
    assert "No page is open" in results[0] and "Sent the screenshot" in results[2]
    shots = [m for m in chat.sent if m.image is not None and m.card is None]
    assert len(shots) == 1 and shots[0].text == "Search results" and shots[0].image.b64 == PNG
    assert any(e.get("image") for e in fe.read_transcript() if e["kind"] == "out")
