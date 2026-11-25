# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Background tasks (SPEC §3.4 "Background tasks", test 23)."""

from __future__ import annotations

import asyncio
import time

import pytest

from aha_local import actions
from aha_local.tools import background
from aha_local.tools.background import Runner

from .conftest import person, settle

IDLE = background.IDLE_AFTER


@pytest.fixture
def runner(agent, registry):
    background.register(registry)
    return Runner(agent.accounts, agent, "Europe/London")


def task(**over) -> dict:
    now = time.time()
    return {
        "id": "abc123",
        "title": "Reply from Sam about Saturday",
        "prompt": "Search the email for a reply from sam@example.com about Saturday; done when one arrives.",
        "every": 15,
        "until": None,
        "next": now - 1,
        "created": int(now) - 3600,
        "checks": 0,
        **over,
    }


def idle(agent, seconds: float = IDLE + 1) -> None:
    agent.last_input = time.time() - seconds


async def test_creating_one_needs_a_card_with_every_detail(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    llm.call(
        "task_create",
        title="Reply from Sam about Saturday",
        prompt="Search the email for a reply from sam@example.com about Saturday.",
        every_minutes=15,
        until="never",
    )
    llm.answer("I'll watch for Sam's reply.")
    await agent.inbound(fe, "tell me when Sam answers")
    card = await chat.next()
    assert card.card["kind"] == "task" and card.card["icon"] == "⏳"
    assert card.card["buttons"] == ["Start", "Edit", "Don't"]
    assert [(f["label"], f["value"]) for f in card.card["fields"]] == [
        ("Waiting for", "Reply from Sam about Saturday"),
        ("How to check", "Search the email for a reply from sam@example.com about Saturday."),
        ("Check every (minutes)", "15"),
        ("Until", "never"),
    ]
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    (saved,) = background.load(fe)
    assert saved["every"] == 15 and saved["until"] is None and saved["next"] > time.time() + 14 * 60


async def test_a_declined_or_too_frequent_task_starts_nothing(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    llm.call("task_create", title="X", prompt="Check x", every_minutes=1, until="never")
    llm.answer("OK.")
    await agent.inbound(fe, "watch x every minute")
    await settle(agent)
    assert "at most every 5 minutes" in str(llm.requests[-1]["messages"][-1])
    llm.call("task_create", title="X", prompt="Check x", every_minutes=5, until="never")
    llm.answer("OK.")
    await agent.inbound(fe, "every 5 then")
    card = await chat.next()
    while card.card is None:
        card = await chat.next()
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)
    assert background.load(fe) == []


async def test_checks_wait_for_idle_and_dont_reset_it(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task()])
    idle(agent, IDLE - 10)  # someone wrote 4 minutes 50 ago
    await runner.tick()
    assert not agent.busy() and not llm.requests

    idle(agent)
    since = agent.last_input
    llm.call("email_search", query="from:sam@example.com")  # not registered: an error, which is fine here
    llm.answer("WAITING: no reply from Sam yet")
    await runner.tick()
    await settle(agent)
    assert "[Background task you set up earlier, check 1" in llm.requests[0]["messages"][-1]["content"]
    assert agent.last_input == since  # a check is no input
    assert not chat.sent and not chat.tools and True not in chat.typed  # quiet while it waits
    (left,) = background.load(fe)
    assert left["last"] == "no reply from Sam yet" and left["checks"] == 1
    assert left["next"] == pytest.approx(time.time() + 15 * 60, abs=5)
    assert all(t["content"] != left["prompt"] for t in fe.load_context()["turns"])  # the turn isn't kept

    # Not due yet: nothing runs, however idle.
    await runner.tick()
    assert len(llm.requests) == 2


async def test_a_closure_goes_to_the_chat_and_ends_the_task(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task(last="no reply yet", checks=3)])
    idle(agent)
    llm.answer("DONE: Sam replied: yes, see you there.")
    await runner.tick()
    await settle(agent)
    assert "Last time you found: no reply yet" in llm.requests[-1]["messages"][-1]["content"]
    assert "check 4" in llm.requests[-1]["messages"][-1]["content"]
    assert chat.texts() == ["_Task done: Reply from Sam about Saturday_\n\nSam replied: yes, see you there."]
    assert background.load(fe) == []

    background.save(fe, [task()])
    llm.answer("**FAILED:** the email account isn't connected any more.")
    await runner.tick()
    await settle(agent)
    assert chat.texts()[-1].startswith("_Task failed: Reply from Sam about Saturday_\n\nthe email account")
    assert background.load(fe) == []


async def test_notify_tells_the_person_and_the_task_goes_on(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task(title="Emails from Sam", last="last email: 'Lunch?'")])
    idle(agent)
    llm.answer("NOTIFY: Sam wrote again: 'Running late, start without me.'")
    await runner.tick()
    await settle(agent)
    assert "NOTIFY:" in llm.requests[-1]["messages"][-1]["content"]
    assert chat.texts() == ["_Task update: Emails from Sam_\n\nSam wrote again: 'Running late, start without me.'"]
    (left,) = background.load(fe)
    assert left["updates"] == 1 and left["checks"] == 1
    assert left["last"] == "Sam wrote again: 'Running late, start without me.'"  # so it isn't reported twice
    assert left["next"] == pytest.approx(time.time() + 15 * 60, abs=5)
    assert background.listing(fe)[0]["updates"] == 1


async def test_a_check_that_doesnt_finish_keeps_waiting(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task()])
    idle(agent)
    for _ in range(agent.config.max_model_calls):
        llm.call("task_list")
    await runner.tick()
    await settle(agent)
    assert not chat.sent and len(background.load(fe)) == 1


async def test_an_expired_task_ends_with_a_message_and_no_model_call(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task(until=time.time() - 60, last="still 'In transit'")])
    agent.last_input = time.time()  # not idle: expiry doesn't wait for idle
    await runner.tick()
    assert not llm.requests and background.load(fe) == []
    assert chat.texts()[0].startswith("_Task expired: Reply from Sam about Saturday_\n\nI stopped waiting on ")
    assert chat.texts()[0].endswith("The last check found: still 'In transit'")


async def test_a_message_stops_a_running_check_and_gets_the_model(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task()])
    idle(agent)
    gate = asyncio.Event()
    real = llm.chat

    async def slow(messages, *a, **kw):
        if "Background task" in str(messages[-1].get("content")):
            await gate.wait()
        return await real(messages, *a, **kw)

    llm.chat = slow
    await runner.tick()
    await asyncio.sleep(0.05)
    assert agent.busy() and not agent.running  # a check runs; no person's task does
    llm.answer("Hello!")
    await agent.inbound(fe, "hi")
    await settle(agent)
    assert chat.texts() == ["Hello!"]
    (left,) = background.load(fe)
    assert left["next"] <= time.time() and left["checks"] == 0  # it runs again at the next idle time
    assert not agent.idle()


async def test_a_card_waiting_for_someone_leaves_the_model_free(runner, agent, llm, chat, accounts):
    """A request waiting days on an unanswered card mustn't hold every check back."""
    fe, hb = person(accounts), accounts.create("Hb")
    llm.call("task_create", title="X", prompt="Check x", every_minutes=5, until="never")
    await agent.inbound(fe, "watch x")
    card = await chat.next()
    assert card.card is not None and agent.running and not agent.busy() and not agent.working()
    background.save(hb, [task()])
    idle(agent)
    llm.answer("WAITING: nothing yet")
    await runner.tick()
    await asyncio.sleep(0.05)
    assert background.load(hb)[0]["checks"] == 1
    background.save(fe, [task(id="fe0001")])
    await runner.tick()  # Fe's own task waits on the card: no check of theirs starts beside it
    assert background.load(fe)[0]["checks"] == 0 and agent.session(fe).background == ""
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)


async def test_one_check_at_a_time_the_most_overdue_first(runner, agent, llm, accounts):
    fe, hb = person(accounts), accounts.create("Hb")
    background.save(fe, [task(next=time.time() - 60)])
    background.save(hb, [task(id="def456", title="Parcel", next=time.time() - 600)])
    idle(agent)
    llm.answer("WAITING: nothing yet")
    await runner.tick()
    await runner.tick()  # the first still runs, or just ended: never two at once
    await settle(agent)
    assert "Waiting for: Parcel" in llm.requests[0]["messages"][-1]["content"]


async def test_the_panel_lists_tasks_and_cancel_is_the_persons(runner, agent, accounts):
    fe = person(accounts)
    background.save(fe, [task(), task(id="def456", title="Parcel", created=1)])
    rows = background.listing(fe)
    assert [r["id"] for r in rows] == ["abc123", "def456"] and rows[0]["every"] == 15 * 60
    assert set(rows[0]) == {"id", "title", "prompt", "every", "until", "next", "last", "checks", "updates", "created"}
    assert actions.cancel_background(fe, "def456", via="settings")
    assert not actions.cancel_background(fe, "def456", via="settings")
    assert [e["id"] for e in background.load(fe)] == ["abc123"]


def test_outcomes():
    assert background.outcome("WAITING: nope") == ("waiting", "nope")
    assert background.outcome("*Done* — here it is") == ("done", "here it is")
    assert background.outcome("failed:\ngone") == ("failed", "gone")
    assert background.outcome("Sam replied.") == ("done", "Sam replied.")
    assert background.outcome("NOTIFY: again") == ("notify", "again")
