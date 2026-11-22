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

from aha_local import checks
from aha_local.agent import Agent
from aha_local.tools import browser, signins
from aha_local.tools.browser import BrowserPool, BrowserSession

from .conftest import person, settle

PASSWORD = "correct-horse-battery"
CODE = "482915"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01 fake").decode()  # 1×1
TOOLS = [
    mt.Tool(name=n, description=n, inputSchema={"type": "object", "properties": {}})
    for n in (
        "browser_navigate",
        "browser_tabs",
        "browser_snapshot",
        "browser_type",
        "browser_take_screenshot",
        "browser_wait_for",
        "browser_click",
        "browser_fill_form",
    )
]


def text_result(text: str, error: bool = False) -> mt.CallToolResult:
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)], isError=error)


LITERAL = re.compile(r'JSON\.parse\(("(?:[^"\\]|\\.)*")\)')
SUBMITTED = "await page.keyboard.press('Enter'); return 'ok'; }"


class FakeContext:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.fields: dict[str, str] = {}
        self.cookies: list[dict] = []
        self.origins: list[dict] = []
        self.calls: list[str] = []
        self.codes: list[str] = []  # harness code run in this context
        self.moves: list[str] = []  # elements the mouse moved to
        self.taps: list[list[tuple[int, int]]] = []
        self.order: list[str] = []  # fields typed or filled, in order
        self.closed = False

    def run_code(self, code: str) -> mt.CallToolResult:
        """What Playwright MCP's browser_run_code_unsafe does with the harness's fixed code."""
        self.codes.append(code)
        echo = f"\n### Ran Playwright code\n```js\n{code}\n```"  # the real one echoes the code: typed text too
        if code in (signins.GET_STATE, signins.GET_STATE_LIGHT):
            state = {"cookies": self.cookies, "origins": [] if "indexedDB" not in code else self.origins}
            return text_result("### Result\n" + json.dumps(json.dumps(state)) + echo)
        if "setStorageState" in code:
            state = json.loads(json.loads(LITERAL.search(code).group(1)))
            self.cookies, self.origins = list(state["cookies"]), list(state["origins"])
        elif "keyboard.type" in code:  # typing one key at a time
            ref = re.search(r"aria-ref=(\w+)", code).group(1)
            self.fields[ref] = json.loads(json.loads(LITERAL.search(code).group(1)))
            self.order.append(f"typed {ref}")
            if code.endswith(SUBMITTED):
                self.cookies.append(
                    {"name": "sid", "value": "s3ss10n", "domain": ".waitrose.com", "path": "/", "expires": -1}
                )
        elif "innerWidth" in code:
            return text_result("### Result\n" + json.dumps(json.dumps([1280, 813])) + echo)
        elif "page.mouse.down" in code:
            points = re.findall(
                r"page\.mouse\.move\((\d+), (\d+), \{ steps: \d+ \}\); await page\.waitForTimeout", code
            )
            self.taps.append([(int(x), int(y)) for x, y in points])
        elif "aria-ref=" in code:
            self.moves.append(re.search(r"aria-ref=(\w+)", code).group(1))
        else:
            return text_result("### Error\nunknown harness code", error=True)
        return text_result('### Result\n"ok"' + echo)

    def snapshot(self) -> str:
        if "login" in self.url or "signin" in self.url:
            return (
                f"### Page\n- Page URL: {self.url}\n### Snapshot\n```yaml\n"
                f'- textbox "Email address" [ref=e1]: {self.fields.get("e1", "")}\n'
                f'- textbox "Password" [ref=e2]: {self.fields.get("e2", "")}\n'
                f'- textbox "Verification code" [ref=e3]: {self.fields.get("e3", "")}\n'
                '- button "Sign in" [ref=e4]\n```'
            )
        if "queue" in self.url:  # an "are you human?" check
            return (
                f"### Page\n- Page URL: {self.url}\n- HTTP status: 403\n### Snapshot\n```yaml\n"
                '- iframe [ref=e1]:\n  - checkbox "I\'m not a robot" [ref=f1e2]\n```'
            )
        return (
            f"### Page\n- Page URL: {self.url}\n### Snapshot\n```yaml\n"
            f'- textbox "Your email" [ref=e1]: {self.fields.get("e1", "")}\n'
            f'- textbox "Postcode" [ref=e3]: {self.fields.get("e3", "")}\n'
            '- button "Continue" [ref=e4]\n```'
        )

    async def call_tool(self, name: str, args: dict) -> mt.CallToolResult:
        self.calls.append(name)
        if name == "browser_navigate":
            self.url = args["url"]
            if "visit=" in self.url:  # a site that remembers the visitor
                self.cookies.append({"name": "visitor", "value": "v1", "domain": ".shop.example", "path": "/"})
                self.origins.append(
                    {"origin": "https://www.shop.example", "localStorage": [{"name": "k", "value": "v"}]}
                )
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
        if name == "browser_fill_form":
            for f in args["fields"]:
                self.fields[f["target"]] = f["value"]
                self.order.append(f"filled {f['target']}")
            return text_result(self.snapshot())
        if name in ("browser_wait_for", "browser_click"):
            return text_result(self.snapshot() if name == "browser_click" else "waited")
        if name == "browser_run_code_unsafe":
            return self.run_code(args["code"])
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
                try:
                    while (item := await self.queue.get()) is not None:
                        fn, fut = item
                        fut.set_result(await fn(ctx))
                finally:
                    ctx.closed = True

        self.Session = Session


@pytest.fixture
async def setup(config, accounts, llm, registry, chat, tmp_path):
    fake = FakeBrowser()
    pool = BrowserPool("x", idle_seconds=1800, session=fake.Session)

    async def opened(account_id, session):
        await signins.restore(accounts.get(account_id), session)

    async def closing(account_id, session):
        await signins.capture(accounts.get(account_id), session)

    pool.on_open, pool.on_close = opened, closing
    assert await browser.register(registry, pool, tmp_path / "pins.json", wait=2) == len(TOOLS)
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
    ]  # no "keep me signed in": the browser remembers, like the person's own (SPEC §3.4)
    assert c["buttons"] == ["Sign in", "Don't"] and c.get("image")
    await agent.approval_reply(fe, c["id"], "approve", ["waitrose.com", "fe@example.com", PASSWORD])
    await settle(agent)
    page = fake.contexts[-1]
    assert page.fields == {"e1": "fe@example.com", "e2": PASSWORD}
    assert "browser_type" not in page.calls and len([c for c in page.codes if "keyboard.type" in c]) == 2  # human pace
    recorded = everything_recorded(agent, llm, chat, fe)
    assert PASSWORD not in recorded  # the typing code echoed it back; that result is never passed on
    # The page echoed it back; the model saw it masked.
    assert "••••" in json.dumps(llm.requests[-1]["messages"], ensure_ascii=False)
    assert "signed in on waitrose.com themselves" in recorded and "stay signed in" in recorded
    assert signins.sites(fe) == [{"site": "waitrose.com", "signed_in": True}]
    raw = (fe.dir / signins.FILE).read_bytes()
    assert b"s3ss10n" not in raw and b"waitrose" not in raw and PASSWORD.encode() not in raw  # encrypted at rest


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


async def test_sign_ins_come_back_only_for_the_same_person_and_can_be_forgotten(setup, llm, chat, accounts):
    agent, pool, fake = setup
    fe, hb = person(accounts), accounts.create("Hb")
    card = await start_sign_in(agent, llm, chat, fe)
    await agent.approval_reply(fe, card.card["id"], "approve", ["waitrose.com", "fe@example.com", PASSWORD])
    await settle(agent)

    await pool.close(fe.id)  # idle timeout: the context is gone…
    await pool.for_account(fe.id).call("browser_tabs", {"action": "list"})
    assert [c["value"] for c in fake.contexts[-1].cookies] == ["s3ss10n"]  # …and comes back signed in
    await pool.for_account(hb.id).call("browser_tabs", {"action": "list"})
    assert fake.contexts[-1].cookies == []  # never into another person's browser

    await agent.inbound(fe, "sign-ins")
    assert "- waitrose.com (signed in)" in chat.sent[-1].text
    fe_context = fake.contexts[-2]
    await agent.inbound(fe, "forget sign-in www.waitrose.com")
    assert "forgotten waitrose.com" in chat.sent[-1].text
    assert signins.sites(fe) == [] and not (fe.dir / signins.FILE).exists()
    assert fe_context.closed and not pool.is_open(fe.id)  # nothing of it is left in memory either
    await agent.inbound(fe, "forget sign-in waitrose.com")
    assert "doesn't remember waitrose.com" in chat.sent[-1].text


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


async def test_pointing_at_a_sign_in_card_without_showing_one_goes_back_once(setup, llm, chat, accounts):
    agent, _, _ = setup
    fe = person(accounts)
    llm.call("browser_navigate", url="https://www.waitrose.com/login")
    llm.answer("Please sign in using the sign-in card that has appeared in the app.")  # no card exists
    llm.call("browser_sign_in", username_target="e1", password_target="e2")
    await agent.inbound(fe, "add toilet paper to my Waitrose basket")
    card = await chat.next()
    assert card.card and card.card["kind"] == "signin"
    assert "haven't called browser_sign_in" in json.dumps(llm.requests[2]["messages"])
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)


async def test_claims_without_the_tool_go_back_once(setup, llm, chat, accounts):
    """Seen with the real model: 'Scheduled…' with no schedule_add; 'Here is the page' with no screenshot sent."""
    from aha_local.tools import schedule

    agent, _, _ = setup
    schedule.register(agent.registry)
    fe = person(accounts)
    llm.answer("Scheduled. Every weekday at 08:00 I'll check the forecast.")
    llm.answer("I can't run things on a schedule yet.")
    await agent.inbound(fe, "every weekday at 8am tell me if it will rain")
    await settle(agent)
    assert "haven't called schedule_add" in json.dumps(llm.requests[1]["messages"])
    assert chat.texts()[-1] == "I can't run things on a schedule yet."

    llm.call("browser_navigate", url="https://news.example.com/")
    llm.answer("Here is the news homepage.")
    llm.call("show_screenshot")
    llm.answer("There it is.")
    await agent.inbound(fe, "show me a screenshot of the news homepage")
    await settle(agent)
    assert "only shows the page to you" in json.dumps(llm.requests[-2]["messages"])
    assert any(m.image is not None and m.card is None for m in chat.sent)

    for request in ("show me how to cook rice", "what time is it?"):
        assert not checks.unshown_screenshot(request, {"browser_navigate"})


def test_asking_for_a_password_in_the_chat_is_caught():
    for bad in (
        "Tell me your email and password and I'll walk you through the sign-in.",
        "Please send me the verification code from the text message.",
        "You can share your Waitrose password here.",
    ):
        assert checks.asks_for_secret(bad), bad
    for fine in (
        "I'll show you the sign-in card again: type your password there.",
        "Your password is never shown to me.",
        "What time should I book?",
    ):
        assert not checks.asks_for_secret(fine), fine


WAITROSE_REFUSAL = (
    "I can navigate to the Waitrose website and help you find toilet paper options, but I can't log into your "
    "personal account or add items to your cart. That would require your login credentials, which I shouldn't ask for."
)


async def test_refusing_to_sign_in_without_trying_goes_back_once(setup, llm, chat, accounts):
    """Seen with the real model (2026-11-22): it refused before opening the site."""
    agent, _, _ = setup
    fe = person(accounts)
    llm.answer(WAITROSE_REFUSAL)
    llm.call("browser_navigate", url="https://www.waitrose.com/login")
    llm.call("browser_sign_in", username_target="e1", password_target="e2")
    await agent.inbound(fe, "can you log into waitrose website and add toilet papers to the cart?")
    card = await chat.next()
    assert card.card and card.card["kind"] == "signin"
    assert WAITROSE_REFUSAL not in chat.texts()
    assert "you can't sign in or use the person's account" in json.dumps(llm.requests[1]["messages"])
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)


def test_what_counts_as_refusing_to_sign_in():
    for bad in (
        WAITROSE_REFUSAL,
        "Sorry, I'm unable to sign in to your Tesco account.",
        "I cannot add the toilet roll to your basket.",
        "Logging in needs your password, so I can't access your account.",
    ):
        assert checks.refuses_sign_in(bad, set(), True), bad
    assert not checks.refuses_sign_in(WAITROSE_REFUSAL, {"browser_sign_in"}, True)  # it tried: an honest report
    assert not checks.refuses_sign_in(WAITROSE_REFUSAL, set(), False)  # no browser on this computer
    for fine in (
        "I couldn't find a sign-in form on that page.",
        "Added 2 packs of toilet paper to your basket.",
        "The site can't be opened right now.",
    ):
        assert not checks.refuses_sign_in(fine, set(), True), fine
