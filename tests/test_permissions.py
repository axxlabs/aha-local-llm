# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Permissions up front (SPEC §3.4 "Permissions up front", §5.2 "Allowed up front", test 24)."""

from __future__ import annotations

import pytest

from aha_local import checks
from aha_local.approvals import Decision, Field
from aha_local.llm import Reply, ToolCall
from aha_local.policy.gate import Action, Facts, decide
from aha_local.policy.grants import Grant
from aha_local.tools import background, guard, permissions
from aha_local.tools.registry import TaskState, ToolContext

from .conftest import person, settle


def tool_results(llm) -> list[str]:
    return [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]


async def test_no_tool_runs_before_the_permissions(agent, llm, chat, accounts):
    llm.permissions_first = False
    llm.call("current_time")
    llm.call("permissions_request")
    llm.call("current_time")
    llm.answer("It's noon.")
    await agent.inbound(person(accounts), "what time is it?")
    await settle(agent)
    first, nothing, ran = tool_results(llm)
    assert first == permissions.FIRST
    assert nothing == permissions.NOTHING  # nothing to allow: no card
    assert ran != permissions.FIRST and not any(m.card for m in chat.sent)


async def test_asked_with_other_calls_it_runs_first(agent, llm, chat, accounts):
    llm.permissions_first = False
    llm.script.append(Reply("", [ToolCall("a", "current_time", "{}"), ToolCall("b", "permissions_request", "{}")]))
    llm.answer("Noon.")
    await agent.inbound(person(accounts), "time?")
    await settle(agent)
    tool_msgs = [m for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["a", "b"]  # in the order they were asked
    assert tool_msgs[0]["content"] != permissions.FIRST  # but it ran after the permissions


async def test_one_card_allows_what_stays_ticked_for_the_topic(agent, llm, chat, accounts):
    fe = person(accounts)
    llm.permissions_first = False
    llm.call(
        "permissions_request",
        type=["https://www.thefork.co.uk/booking"],
        open=["thefork.co.uk", "linkedin.com"],
        email=["Sam@Example.com"],
        search=True,
    )
    llm.answer("Booking it.")
    await agent.inbound(fe, "book us a table and tell Sam")
    card = await chat.next()
    assert card.card["kind"] == "permissions" and card.card["icon"] == "🔓"
    assert card.card["buttons"] == ["Allow", "Ask each time"]
    labels = [f["label"] for f in card.card["fields"]]
    assert labels == [  # typing on thefork.co.uk includes opening it: once
        "Type and fill in forms on thefork.co.uk (searches, sign-ups, bookings)",
        "Open pages on linkedin.com (read only), even after reading your email or private pages",
        "Search the web, even after reading your email or private pages",
        "Send email to sam@example.com",
    ]
    assert all(f["kind"] == "bool" and f["value"] == "true" for f in card.card["fields"])
    await agent.approval_reply(fe, card.card["id"], "approve", ["true", "true", "false", "true"])
    await settle(agent)
    (result,) = tool_results(llm)
    assert result.startswith("Allowed up front: Type and fill in forms on thefork.co.uk")
    assert "Not allowed (asked when it happens): Search the web" in result
    grant = agent.session(fe).grant
    assert grant.type == {"thefork.co.uk"} and grant.open == {"linkedin.com"}
    assert grant.email == {"sam@example.com"} and not grant.search

    # The next message in the same topic: what's allowed isn't asked again.
    llm.call("permissions_request", type=["thefork.co.uk"], open=["linkedin.com", "thefork.co.uk"])
    llm.answer("Done.")
    await agent.inbound(fe, "make it 8pm")
    await settle(agent)
    assert tool_results(llm)[-1] == permissions.NOTHING
    assert len([m for m in chat.sent if m.card]) == 1


async def test_declining_allows_nothing_and_a_new_topic_starts_clean(agent, llm, chat, accounts):
    fe = person(accounts)
    llm.permissions_first = False
    llm.call("permissions_request", calendar_add=True)
    llm.answer("OK.")
    await agent.inbound(fe, "put it in my calendar")
    card = await chat.next()
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)
    assert tool_results(llm)[-1].startswith(permissions.DECLINED)
    assert not agent.session(fe).grant
    agent.session(fe).grant.add(Grant(type={"x.com"}))
    assert not agent.session(fe).grant_for(("another", "topic"))


async def test_trusted_sites_and_bad_names_are_never_on_the_card(agent, llm, chat, accounts):
    from aha_local.tools import trusted

    fe = person(accounts)
    trusted.set_trusted(fe, "waitrose.com", True)
    llm.permissions_first = False
    llm.call("permissions_request", type=["not a site"])
    llm.call("permissions_request", type=["waitrose.com"], email=["two@a.com three@b.com"])
    llm.call("permissions_request", type=["waitrose.com"], open=["waitrose.com"])
    llm.answer("OK.")
    await agent.inbound(fe, "order the usual")
    await settle(agent)
    bad, two, trusted_only = tool_results(llm)
    assert bad.startswith("Not asked: not a site") and "Not asked: two@a.com three@b.com" in two
    assert trusted_only == permissions.NOTHING and not any(m.card for m in chat.sent)


def test_the_gate_runs_what_was_allowed_and_nothing_else():
    g = Grant(open={"linkedin.com"}, type={"thefork.co.uk"}, search=True, email={"sam@example.com"}, calendar_add=True)
    f = Facts(private=True, granted=g)
    assert decide(Action("type", site="thefork.co.uk"), f).why == "granted"
    assert decide(Action("open", site="thefork.co.uk", url="https://thefork.co.uk/?name=Fe"), f).why == "granted"
    assert decide(Action("open", site="thefork.co.uk", url="https://thefork.co.uk/r/1"), f).why == "granted"
    assert decide(Action("type", site="opentable.com"), f).outcome == "card"
    # Opening allowed is reading only: no typing there, and no address that carries data to it.
    assert decide(Action("open", site="linkedin.com", url="https://linkedin.com/in/sam"), f).why == "granted"
    assert decide(Action("type", site="linkedin.com"), f).outcome == "card"
    assert decide(Action("open", site="linkedin.com", url="https://linkedin.com/?q=Fe"), f).outcome == "card"
    closed = Facts(allow_all_sites=False, granted=g)
    assert decide(Action("open", site="linkedin.com", url="https://linkedin.com/"), closed).why == "granted"
    assert decide(Action("open", site="bbc.co.uk", url="https://bbc.co.uk/"), closed).outcome == "card"
    assert decide(Action("search"), f).why == "granted"
    email = lambda *to: Action("send", what="email", to=to)  # noqa: E731
    assert decide(email("sam@example.com"), f).why == "granted"
    assert decide(email("sam@example.com", "ana@example.com"), f).outcome == "card"  # everyone, or a card
    assert decide(email(), f).outcome == "card"
    assert decide(Action("send", what="calendar", to=()), f).why == "granted"
    assert decide(Action("send", what="calendar", to=("ana@example.com",)), f).outcome == "card"
    # Adding isn't changing, and a draft isn't a send.
    assert decide(Action("send", what="calendar_change", to=()), f).outcome == "card"
    assert decide(Action("send", what="draft", to=("ana@example.com",)), f).outcome == "card"
    assert decide(Action("send", what="draft", to=("sam@example.com",)), f).why == "granted"
    drafts = Facts(granted=Grant(drafts=True))
    assert decide(Action("send", what="draft", to=("ana@example.com",)), drafts).why == "granted"
    assert decide(Action("send", what="email", to=("ana@example.com",)), drafts).outcome == "card"
    for what in ("memory", "task", "calendar_delete"):  # nothing allows these: always their own card
        assert decide(Action("send", what=what, to=()), f).outcome == "card"
    assert decide(Action("send", site="whatsapp.com"), f).outcome == "card"  # a message typed on a site
    assert decide(Action("search"), Facts(private=True)).outcome == "card"


class Asked:
    def __init__(self) -> None:
        self.cards: list[str] = []

    async def ask(self, account, kind, title, fields, image=None, why=""):
        self.cards.append(kind)
        return Decision(True, [f.value for f in fields])


async def test_an_allowed_send_skips_its_card_and_says_what_went(accounts, config):
    said: list[str] = []

    async def send(text, image=None):
        said.append(text)

    fe = person(accounts)
    asked = Asked()
    state = TaskState(grant=Grant(email={"sam@example.com"}))
    ctx = ToolContext(fe, asked, config, state, None, send)  # type: ignore[arg-type]
    fields = [Field("To", "Sam <sam@example.com>"), Field("Subject", "Saturday"), Field("Message", "Hi", "longtext")]
    d, refused = await guard.send(ctx, "email", "Send this email?", fields, to=["sam@example.com"])
    assert d.approved and refused is None and d.values == ["Sam <sam@example.com>", "Saturday", "Hi"]
    assert asked.cards == [] and said == [
        "✉️ Send this email, as you allowed up front. To: Sam <sam@example.com>; Subject: Saturday"
    ]
    assert any('"granted"' in str(line) for line in fe.vault.records(fe.dir / "audit.jsonl"))
    await guard.send(ctx, "email", "Send this email?", fields, to=["ana@example.com"])
    await guard.send(ctx, "memory", "Remember this?", [Field("Fact", "x")])
    assert asked.cards == ["email", "memory"]


async def test_a_background_task_keeps_what_was_allowed(agent, llm, chat, accounts, registry):
    background.register(registry)
    fe = person(accounts)
    agent.session(fe).grant_for(None)
    llm.permissions_first = False
    llm.call("permissions_request", open=["royalmail.com"])
    llm.call(
        "task_create", title="Parcel", prompt="Open royalmail.com tracking for AB123.", every_minutes=60, until="never"
    )
    llm.answer("Watching it.")
    await agent.inbound(fe, "tell me when my parcel moves")
    allow = await chat.next()
    await agent.approval_reply(fe, allow.card["id"], "approve", ["true"])
    card = await chat.next()
    assert card.card["kind"] == "task"
    assert card.card["fields"][-1] == {
        "label": "Allowed up front",
        "value": "Open pages on royalmail.com (read only), even after reading your email or private pages",
        "editable": False,
        "kind": "info",
    }
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    (entry,) = background.load(fe)
    assert background.grant_of(fe, entry["id"]).open == {"royalmail.com"}
    assert background.listing(fe)[0]["allowed"] == [card.card["fields"][-1]["value"]]


@pytest.mark.parametrize(
    "answer, plan",
    [
        (
            "1. Find a table at 19:00\n2. Book it on thefork.co.uk\n3. I'll remind you every day\n\nShall I go ahead?",
            True,
        ),
        ("I'll remind you every day at 8.", False),
        ("1. Done\n2. Done\n\nAll set.", False),
    ],
)
def test_a_plan_isnt_a_claim_of_work_done(answer, plan):
    assert checks.is_plan(answer) is plan
    assert checks.unbacked_schedule_claim(answer, set()) is (not plan and "remind" in answer)


async def test_change_the_plan_allows_nothing_and_sends_it_back_to_its_plan(agent, llm, chat, accounts):
    fe = person(accounts)
    llm.permissions_first = False
    llm.call("permissions_request", type=["thefork.co.uk"], email=["sam@example.com"])
    llm.answer("What would you like to change?")
    await agent.inbound(fe, "book us a table and tell Sam")
    card = await chat.next()
    assert card.card["replan"] == "Change the plan" and card.card["buttons"] == ["Allow", "Ask each time"]
    await agent.approval_reply(fe, card.card["id"], "replan")
    await settle(agent)
    assert tool_results(llm)[-1] == permissions.REPLAN
    assert not agent.session(fe).grant
    assert chat.sent[-1].text == "What would you like to change?"


async def test_only_a_card_that_offers_it_can_be_sent_back_to_its_plan(agent, llm, chat, accounts):
    fe = person(accounts)
    llm.call("memory_add", text="Likes tea")
    llm.answer("OK.")
    await agent.inbound(fe, "I like tea")
    card = await chat.next()
    assert "replan" not in card.card
    await agent.approval_reply(fe, card.card["id"], "replan")  # ignored: the card still waits
    assert agent.approvals.pending_for(fe.address) is not None
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)


def test_a_task_stored_before_the_split_keeps_what_it_allowed():
    g = Grant.from_dict({"sites": ["royalmail.com"], "email": ["sam@example.com"], "calendar": True})
    assert g.type == {"royalmail.com"} and g.email == {"sam@example.com"} and g.calendar_add and g.calendar_change
    assert Grant.from_dict(g.to_dict()) == g


async def test_changing_an_event_needs_its_own_permission(accounts, config):
    asked = Asked()
    ctx = ToolContext(person(accounts), asked, config, TaskState(grant=Grant(calendar_add=True)), None, None)  # type: ignore[arg-type]
    fields = [Field("Title", "Dinner")]
    await guard.send(ctx, "calendar", "Add this?", fields, to=[])
    await guard.send(ctx, "calendar", "Change “Dinner”?", fields, to=[], what="calendar_change")
    assert asked.cards == ["calendar"]
