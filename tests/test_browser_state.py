# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
# ruff: noqa: F811  (`setup` is test_signin's fixture)
"""The browser remembers, looks like a person's, and lets the person do human checks (SPEC §3.4, §5.6; tests 13,
20, 21). Uses the fake Playwright MCP of test_signin."""

from __future__ import annotations

import json
import re

import pytest

from aha_local import approvals
from aha_local.tools import human, signins
from aha_local.tools.registry import TaskState

from .conftest import person, settle
from .test_signin import PASSWORD, setup, start_sign_in  # noqa: F401


def tool_results(llm) -> list[str]:
    return [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]


async def test_any_site_is_remembered_between_tasks_and_restarts(setup, llm, chat, accounts):
    """SPEC §3.4 "Browser profiles": the browser keeps everything in its own profile; connecting again (after an
    agent restart) finds it as it was, IndexedDB included. Nothing is exported or saved by aha."""
    agent, pool, fake = setup
    fe = person(accounts)
    llm.call("browser_navigate", url="https://www.shop.example/visit-1/")
    llm.answer("Opened.")
    await agent.inbound(fe, "open the shop")
    await settle(agent)
    assert await signins.sites(fe, pool.for_account(fe.id)) == [{"site": "shop.example", "signed_in": False}]
    first = fake.contexts[-1]
    first.origins.append({"origin": "https://web.whatsapp.com", "indexedDB": [{"name": "wawc"}]})
    await pool.close(fe.id)  # the agent restarting: the connection goes, the browser and its profile stay
    assert first.closed
    await pool.for_account(fe.id).call("browser_tabs", {"action": "list"})
    again = fake.contexts[-1]
    assert again is first and [c["value"] for c in again.cookies] == ["v1"]  # the same profile
    assert {"origin": "https://web.whatsapp.com", "indexedDB": [{"name": "wawc"}]} in again.origins
    assert not any("storageState" in c or "setStorageState" in c for c in again.codes)  # nothing exported


async def test_clear_browser_deletes_the_whole_profile(setup, llm, chat, accounts):
    agent, pool, fake = setup
    fe, hb = person(accounts), accounts.create("Hb")
    for who in (fe, hb):
        llm.call("browser_navigate", url="https://www.shop.example/visit-1/")
        llm.answer("Opened.")
        await agent.inbound(who, "open the shop")
        await settle(agent)
    await agent.inbound(fe, "clear browser")
    assert "forgotten every site" in chat.sent[-1].text
    assert ("/forget", fe.id) in fake.calls and fe.id not in fake.profiles  # the profile folder is deleted
    assert not (fe.dir / signins.FILE).exists() and not pool.is_open(fe.id)
    assert hb.id in fake.profiles and pool.is_open(hb.id)  # another person's browser is untouched
    await pool.for_account(fe.id).call("browser_tabs", {"action": "list"})
    assert fake.contexts[-1].cookies == []  # a new, empty profile


async def test_after_a_signed_in_page_a_new_site_is_a_card(setup, llm, chat, accounts):
    """SPEC §5.2 "Private data": from then on in the task, a site not opened yet needs the person's approval."""
    agent, _, _ = setup
    fe = person(accounts)
    card = await start_sign_in(agent, llm, chat, fe)
    await agent.approval_reply(fe, card.card["id"], "approve", ["waitrose.com", "fe@example.com", PASSWORD, "false"])
    await settle(agent)

    chat.read = len(chat.sent)
    llm.call("browser_navigate", url="https://www.waitrose.com/orders")  # signed in: its own pages stay auto
    llm.call("browser_navigate", url="https://www.waitrose.com/basket")
    llm.call("browser_navigate", url="https://evil.example/")
    llm.answer("Done.")
    await agent.inbound(fe, "what did I order last week?")
    card = await chat.next()
    assert card.card and card.card["title"] == "Open evil.example?"
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)
    assert "the user stopped it" in tool_results(llm)[-1]

    llm.call("browser_navigate", url="https://news.example/")  # a new task that never saw a signed-in page
    llm.answer("Opened.")
    await agent.inbound(fe, "open the news")
    await settle(agent)
    assert not any(m.card for m in chat.sent[chat.read :])


async def test_typing_and_clicks_go_at_human_pace(setup, llm, chat, accounts):
    agent, _, fake = setup
    fe = person(accounts)
    llm.call("browser_navigate", url="https://shop.example/")
    llm.call("browser_click", element="Sign in", target="e4")
    llm.call("browser_type", element="Email", target="[ref=e1]", text="fe@example.com")
    llm.answer("Typed.")
    await agent.inbound(fe, "type my email")
    card = await chat.next()
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    ctx = fake.contexts[-1]
    assert ctx.moves[:1] == ["e4"] and ctx.calls.count("browser_click") == 1  # the mouse got there first
    assert ctx.fields == {"e1": "fe@example.com"} and "browser_type" not in ctx.calls


def test_only_element_references_go_into_harness_code():
    assert human.is_ref("e12") and human.is_ref("f1e3")
    for bad in ("e1'); evil('", "#id", "button", "", None, "e1 ", "aria-ref=e1"):
        assert not human.is_ref(bad), bad
        with pytest.raises(ValueError):
            human.move_code(bad)  # type: ignore[arg-type]
    code = human.type_code("e5", "a\"b'c`d${x}\n", True)
    literal = re.search(r'JSON\.parse\(("(?:[^"\\]|\\.)*")\)', code).group(1)
    assert json.loads(json.loads(literal)) == "a\"b'c`d${x}\n"  # text stays data
    assert human.parse_taps("10,20;1279,812", 1280, 813) == [(10, 20), (1279, 812)]
    for bad in ("1280,5", "5,813", "x,y", "1,2,3", ";".join(["1,1"] * 21)):
        assert human.parse_taps(bad, 1280, 813) is None, bad
    assert human.parse_taps("", 1280, 813) == []


def test_a_check_cards_taps_are_checked_like_other_values():
    fields = [approvals.Field("Site", "x.example", "info"), approvals.Field("Taps", "", "taps")]
    assert approvals.checked_values(fields, ["x.example", "10,20;30,40"]) == ["x.example", "10,20;30,40"]
    assert approvals.checked_values(fields, ["x.example", ""]) == ["x.example", ""]
    assert approvals.checked_values(fields, ["x.example", "click the bus"]) is None
    assert approvals.checked_values(fields, ["y.example", "1,1"]) is None  # info fields can't change


async def test_the_person_does_the_check_by_tapping_the_screenshot(setup, llm, chat, accounts):
    agent, _, fake = setup
    fe = person(accounts)
    llm.call("browser_navigate", url="https://tickets.example/queue")
    llm.call("browser_human_check")
    llm.answer("Through the check.")
    await agent.inbound(fe, "get me in the ticket queue")
    card = (await chat.next()).card
    assert card["kind"] == "check" and card["title"] == "Show tickets.example you're human" and card["image"]
    assert card["buttons"] == ["Send taps", "Give up"]
    assert [(f["label"], f["kind"], f["editable"]) for f in card["fields"]] == [
        ("Site", "info", False),
        ("Taps", "taps", True),
    ]
    await agent.approval_reply(fe, card["id"], "approve", ["tickets.example", "1,1"])  # outside the 1×1 screenshot
    card = (await chat.next()).card  # nothing clicked: a fresh card
    assert fake.contexts[-1].taps == []
    await agent.approval_reply(fe, card["id"], "approve", ["tickets.example", "0,0"])
    card = (await chat.next()).card  # tapped, then a new screenshot
    assert len(fake.contexts[-1].taps) == 1
    await agent.approval_reply(fe, card["id"], "approve", ["tickets.example", ""])  # "It's done"
    await settle(agent)
    result = tool_results(llm)[-1]
    assert "did the check" in result and "0,0" not in result
    assert chat.texts()[-1] == "Through the check."


async def test_no_check_on_the_page_is_refused_once(setup, llm, chat, accounts):
    """Seen: with no check on WhatsApp, the model called browser_human_check to have the person tap away the
    signature. Without a check found on the page it's refused once; asked again (the detector can miss one), the
    card shows."""
    agent, _, _ = setup
    fe = person(accounts)
    llm.call("browser_navigate", url="https://example.com/")
    llm.call("browser_human_check")
    llm.call("browser_human_check")
    llm.answer("Done.")
    await agent.inbound(fe, "open example.com")
    card = (await chat.next()).card
    assert card["kind"] == "check"
    assert 'No "are you human?" check was found on this page' in tool_results(llm)[1]
    await agent.approval_reply(fe, card["id"], "decline")
    await settle(agent)


async def test_giving_up_on_a_check_stops_it(setup, llm, chat, accounts):
    agent, _, _ = setup
    fe = person(accounts)
    llm.call("browser_human_check")  # nothing open: refused
    llm.call("browser_navigate", url="https://tickets.example/queue")
    llm.call("browser_human_check")
    llm.answer("Couldn't get past it.")
    await agent.inbound(fe, "get me in the ticket queue")
    card = (await chat.next()).card
    await agent.approval_reply(fe, card["id"], "decline")
    await settle(agent)
    results = tool_results(llm)
    assert "No page is open" in results[0] and "gave up on the check" in results[-1]


async def test_an_approved_form_is_typed_in_its_own_order(setup, llm, chat, accounts):
    agent, _, fake = setup
    fe = person(accounts)
    fields = [
        {"element": "Email", "target": "e1", "type": "textbox", "value": "fe@example.com", "name": "Email"},
        {"element": "Country", "target": "e9", "type": "combobox", "value": "Portugal", "name": "Country"},
        {"element": "Code", "target": "e3", "type": "textbox", "value": "1000-001", "name": "Code"},
    ]
    llm.call("browser_navigate", url="https://shop.example/")
    llm.call("browser_fill_form", fields=fields)
    llm.answer("Filled.")
    await agent.inbound(fe, "fill in the form")
    card = await chat.next()
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    assert fake.contexts[-1].order == ["typed e1", "filled e9", "typed e3"]


def test_sign_in_forms_are_spotted_from_the_page():
    from aha_local.tools.browser_page import is_sign_in_form

    password = '- textbox "Password" [ref=e2]'
    email_step = (
        '- heading "Sign in" [level=1] [ref=e1]\n- textbox "Email address" [ref=e2]\n- button "Continue" [ref=e3]'
    )
    assert is_sign_in_form(password, "https://www.waitrose.com/ecom/shop")
    assert is_sign_in_form(email_step, "https://www.waitrose.com/ecom/shop")
    assert is_sign_in_form('- textbox "Email" [ref=e1]', "https://shop.example/account/login?next=/")
    newsletter = '- textbox "Email address" [ref=e7]\n- button "Subscribe" [ref=e8]'
    assert not is_sign_in_form(newsletter, "https://shop.example/")  # an email field alone isn't a sign-in form
    assert not is_sign_in_form('- link "Sign in" [ref=e1]\n- button "Add to trolley" [ref=e2]', "https://shop.example/")


async def test_a_remembered_session_that_ended_is_said_plainly_and_not_glossed_over(setup, llm, chat, accounts):
    """Seen with the real model (2026-11-22): Waitrose was remembered, its session had ended, the model assumed not."""
    agent, _, _ = setup
    fe = person(accounts)
    signins.signed_in(fe, "waitrose.com", "waitrose.com")  # signed in once; the session has since ended
    llm.call("browser_navigate", url="https://www.waitrose.com/login")  # the fake page has a password field
    llm.answer("You're signed in to Waitrose, I've added the toilet paper.")  # glosses over the sign-in page
    llm.call("browser_login")
    await agent.inbound(fe, "add toilet paper to my Waitrose trolley")
    card = await chat.next()
    assert card.card and card.card["kind"] == "signin"
    sent_back = json.dumps(llm.requests[1]["messages"])
    assert "that session has ended: they are NOT signed in there now" in sent_back  # said in the tool result
    assert "The last page you saw is a sign-in form on waitrose.com" in json.dumps(llm.requests[2]["messages"])
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)
    assert chat.texts()[-1] != "You're signed in to Waitrose, I've added the toilet paper."


async def test_a_human_check_on_the_page_is_said_and_a_card_follows(setup, llm, chat, accounts):
    """Seen with the real model (2026-11-22): it said a captcha came up but never showed the check card."""
    agent, _, _ = setup
    fe = person(accounts)
    llm.call("browser_navigate", url="https://tickets.example/queue")  # HTTP 403 with a reCAPTCHA box
    llm.answer("The site is showing a CAPTCHA, so I couldn't get in.")  # tells, but shows no card
    llm.call("browser_human_check")
    await agent.inbound(fe, "get me in the ticket queue")
    card = (await chat.next()).card
    assert card and card["kind"] == "check"
    first = tool_results(llm)[0]
    assert first.startswith('[Aha!: this page shows an "are you human?" check') and "can't tell why" not in first
    assert "you haven't called browser_human_check" in json.dumps(llm.requests[2]["messages"])
    await agent.approval_reply(fe, card["id"], "decline")
    await settle(agent)


def test_human_checks_are_spotted_from_the_page():
    from aha_local.tools.browser_page import CHALLENGE

    for page in (
        '- checkbox "I\'m not a robot" [ref=f1e2]',
        '- iframe "reCAPTCHA" [ref=e4]',
        '- checkbox "Verify you are human" [ref=f2e1]',
        "- text: Press & Hold to confirm you are a human (and not a bot).",
        '- heading "Our systems have detected unusual traffic from your computer network" [level=1]',
    ):
        assert CHALLENGE.search(page), page
    for page in ('- button "Add to trolley"', '- link "Robots and drones" [ref=e9]', '- heading "Human resources"'):
        assert not CHALLENGE.search(page), page


async def test_on_a_trusted_site_typing_runs_without_a_card(setup, llm, chat, accounts):
    """SPEC §5.7: the person's own choice, in Settings; the model isn't told."""
    from aha_local.tools import trusted

    agent, _, fake = setup
    fe, hb = person(accounts), accounts.create("Hb")
    assert trusted.set_trusted(fe, "https://www.shop.example/basket", True)
    assert trusted.sites(fe) == ["shop.example"] and trusted.sites(hb) == []
    assert not trusted.set_trusted(fe, "localhost", True) and not trusted.set_trusted(fe, "nonsense", True)
    llm.call("browser_navigate", url="https://www.shop.example/")
    llm.call("browser_type", element="Email", target="e1", text="fe@example.com")
    llm.answer("Typed.")
    await agent.inbound(fe, "type my email")
    await settle(agent)
    assert not any(m.card for m in chat.sent)
    assert fake.contexts[-1].fields == {"e1": "fe@example.com"}
    assert tool_results(llm)[1].startswith("Typed: “fe@example.com”.")  # as with a card: nothing about trust
    assert "trust" not in tool_results(llm)[1].lower()
    audit = b"\n".join(fe.vault.records(fe.dir / "audit.jsonl")).decode()
    assert '"trusted": "shop.example"' in audit and "fe@example.com" not in audit

    chat.read = len(chat.sent)
    llm.call("browser_navigate", url="https://www.shop.example/")  # another person: their own list, so a card
    llm.call("browser_type", element="Email", target="e1", text="hb@example.com")
    llm.answer("Typed.")
    await agent.inbound(hb, "type my email")
    card = await chat.next()
    assert card.card and card.card["kind"] == "action"
    await agent.approval_reply(hb, card.card["id"], "decline")
    await settle(agent)


async def test_a_card_is_never_shown_for_a_call_that_would_fail(setup, llm, chat, accounts):
    """Seen with the real model (2026-11-22): it wrote "ref=f8e31"; the person approved, the browser refused the
    selector, the model retried, and the person was asked the same thing twice."""
    from aha_local.tools.browser_page import normalise_targets

    assert normalise_targets({"target": "ref=f8e31"})["target"] == "f8e31"
    assert normalise_targets({"target": "[ref=e12]"})["target"] == "e12"
    agent, _, fake = setup
    fe = person(accounts)
    llm.call("browser_navigate", url="https://www.shop.example/")
    llm.call("browser_type", element="Search", target="ref=e8e31", text="basmati rice")  # not on the page
    llm.call("browser_type", element="Search", target="ref=f8e31", text="basmati rice")
    llm.answer("Searched.")
    await agent.inbound(fe, "search for basmati rice")
    card = await chat.next()
    assert card.card and card.card["title"] == "Type into Search on shop.example?"  # one card, the page's label
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    assert "e8e31 isn't in the latest snapshot" in tool_results(llm)[1]
    assert fake.contexts[-1].fields == {"f8e31": "basmati rice"}
    assert sum(1 for m in chat.sent if m.card) == 1


async def test_password_fields_are_only_ever_filled_through_browser_login(setup, llm, chat, accounts):
    """Seen with the real model (2026-11-22): a login form as an ordinary form card, a real password typed into it."""
    agent, _, _ = setup
    fe = person(accounts)
    fields = [
        {"element": "Email", "target": "e1", "type": "textbox", "value": "test@test.com", "name": "Email"},
        {"element": "Password", "target": "e7", "type": "textbox", "value": "password123", "name": "Password"},
    ]
    llm.call("browser_navigate", url="https://www.shop.example/account")
    llm.call("browser_fill_form", fields=fields)
    llm.call("browser_type", element="Password", target="e7", text="password123")
    llm.answer("Couldn't.")
    await agent.inbound(fe, "log me in")
    await settle(agent)
    assert not any(m.card for m in chat.sent)
    results = tool_results(llm)
    assert all("is a sign-in field" in r and "browser_login" in r for r in results[1:3])


async def test_a_new_windows_blank_page_is_said_and_the_last_page_given(setup, llm, chat, accounts):
    """Seen: after the browser closed for being idle, the model saw about:blank, said "the session timed out" and
    told the person to check the site themselves. The state was all there."""
    agent, pool, _ = setup
    fe = person(accounts)
    llm.call("browser_navigate", url="https://www.shop.example/trolley/visit-1/")
    llm.answer("Opened.")
    await agent.inbound(fe, "open my trolley")
    await settle(agent)
    assert signins.last_page(fe) == "https://www.shop.example/trolley/visit-1/"
    await pool.close(fe.id)  # the agent restarting: the window comes back blank
    assert signins.last_page(fe) == "https://www.shop.example/trolley/visit-1/"

    n = len(llm.requests)
    llm.call("browser_snapshot")
    llm.answer("The session timed out; please check the site yourself.")
    llm.call("browser_navigate", url="https://www.shop.example/trolley")
    llm.answer("Your trolley has the rice in it.")
    await agent.inbound(fe, "remove the toilet paper")
    await settle(agent)
    results = [m for m in llm.requests[n + 1]["messages"] if m.get("role") == "tool"]
    assert "new browser window" in results[-1]["content"]
    assert "https://www.shop.example/trolley/visit-1/" in results[-1]["content"]
    retry = llm.requests[n + 2]["messages"][-1]["content"]
    assert retry.startswith("[aha check]") and "blank page of a new browser window" in retry
    assert chat.texts()[-1] == "Your trolley has the rice in it."

    assert await agent.browser_actions.forget_site(fe, "shop.example", via="chat") and signins.last_page(fe) == ""


async def test_on_whatsapp_web_the_message_box_is_always_a_card_and_the_page_is_private(setup, llm, chat, accounts):
    """SPEC §5.2: typing into a messaging site's message box sends a message to someone: a card, trusted or not.
    The search box there follows trust; and the chats are the person's private data."""
    from aha_local.tools import trusted

    agent, _, fake = setup
    fe = person(accounts)
    assert trusted.set_trusted(fe, "web.whatsapp.com", True)
    llm.call("browser_navigate", url="https://web.whatsapp.com/")
    llm.call("browser_type", element="Search", target="e1", text="Sam")
    llm.call("browser_type", element="Message", target="e2", text="On my way")
    llm.answer("Sent.")
    await agent.inbound(fe, "tell Sam on WhatsApp I'm on my way")
    card = await chat.next()
    assert card.card["title"] == "Send this message on whatsapp.com?"
    signed = "On my way\n(Written by Aha!, Fe's Private AI Assistant)"  # signed in code (SPEC §3.4)
    assert [(f["label"], f["value"], f["kind"]) for f in card.card["fields"]] == [("Text", signed, "longtext")]
    assert card.card["why"].startswith("Sending a message to someone always asks first")  # the card's (i)
    assert fake.contexts[-1].fields == {"e1": "Sam"}  # the search ran without a card: the site is trusted
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    assert fake.contexts[-1].fields == {"e1": "Sam", "e2": signed}
    typed = [c for c in fake.contexts[-1].codes if "aria-ref=e2" in c and "keyboard.type" in c][-1]
    assert "press('Shift+Enter')" in typed and "press('Enter')" not in typed  # a new line mustn't send it early
    assert sum(1 for m in chat.sent if m.card) == 1
    # Seen: the model took the signature for a mistake and tried to have it deleted: it's told it's meant.
    typed_result = next(r for r in tool_results(llm) if r.startswith("Typed: “On my way"))
    assert "added by Aha! on purpose and approved by the person" in typed_result
    assert "on purpose" not in next(r for r in tool_results(llm) if r.startswith("Typed: “Sam"))
    from aha_local.messages import signed as sign

    assert sign(signed, "Fe", medium="chat") == signed  # never signed twice


async def test_a_message_box_on_any_site_is_a_signed_card(setup, llm, chat, accounts):
    """Seen 2026-11-26: LinkedIn messages went unsigned, as ordinary typing on a site, since only WhatsApp was on
    a list. The field's own label says it's a message box."""
    from aha_local.tools import trusted

    agent, _, fake = setup
    fe = person(accounts)
    assert trusted.set_trusted(fe, "www.linkedin.com", True)
    llm.call("browser_navigate", url="https://www.linkedin.com/messaging/")
    llm.call("browser_type", element="Search", target="e1", text="Sam")
    llm.call("browser_type", element="message", target="e2", text="Thanks, see you then")
    llm.answer("Sent.")
    await agent.inbound(fe, "reply to Sam on LinkedIn")
    card = await chat.next()
    assert card.card["title"] == "Send this message on linkedin.com?"
    signed = "Thanks, see you then\n(Written by Aha!, Fe's Private AI Assistant)"
    assert [f["value"] for f in card.card["fields"]] == [signed]
    assert fake.contexts[-1].fields == {"e1": "Sam"}  # the search box follows trust: no card
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    assert fake.contexts[-1].fields == {"e1": "Sam", "e2": signed}


def test_a_whatsapp_web_page_makes_the_task_private(accounts):
    from aha_local.tools.browser_page import note_page
    from aha_local.tools.registry import ToolContext

    ctx = ToolContext(person(accounts), None, None, TaskState())
    note_page(ctx, "### Page\n- Page URL: https://web.whatsapp.com/\n")
    assert ctx.task.private  # their messages: new sites and searches now ask first (SPEC §5.2)


def test_the_mouse_bends_inside_the_element_so_a_hover_button_stays():
    """Seen 2026-11-26: WhatsApp's "Open message options" shows only while the mouse is over the message; a
    swing 40-160 px out before the click made it go, and every click on it failed."""
    import random
    import re

    for seed in range(50):
        code = human.move_code("f66e2346", random.Random(seed))
        first = re.search(r"page\.mouse\.move\(x \+ b\.width \* (-?[\d.]+), y \+ b\.height \* (-?[\d.]+)", code)
        assert first and all(abs(float(v)) <= 0.35 for v in first.groups()), code
