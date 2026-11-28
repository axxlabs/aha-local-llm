# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topics that wait (SPEC §4.1, test 23): a topic sent to the background wakes up on its own, inside itself."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from aha_local import actions, context, topics
from aha_local.tools import background, guard
from aha_local.tools.background import Runner
from aha_local.tools.registry import TaskState

from .conftest import person, settle

IDLE = background.IDLE_AFTER
LONDON = ZoneInfo("Europe/London")


@pytest.fixture
def runner(agent, registry):
    background.register(registry)
    return Runner(agent.accounts, agent, "Europe/London")


def schedule(**over) -> dict:
    now = time.time()
    return {
        "title": "Reply from Sam about Saturday",
        "prompt": "Search the email for a reply from sam@example.com about Saturday.",
        "done_when": "Sam replied about Saturday",
        "every": 15,
        "next": now - 1,
        "created": int(now) - 3600,
        "checks": 0,
        **over,
    }


def waiting_topic(account, name: str = "Saturday with Sam", **over) -> int:
    """A topic already checking on its own, with the person's request as its first turn."""
    n = topics.new(account, title=name)
    topics.update(account, n, lambda e: {**e, "schedule": schedule(**over), "state": "scheduled"})
    context.record_turn(account, n, "Tell me when Sam answers about Saturday", "I'll watch for Sam's reply.")
    return n


def idle(agent, seconds: float = IDLE + 1) -> None:
    agent.last_input = time.time() - seconds


async def approve_next_card(agent, chat, account, decision: str = "approve"):
    card = await chat.next()
    while card.card is None:
        card = await chat.next()
    await agent.approval_reply(account, card.card["id"], decision)
    return card


async def test_deferring_needs_a_card_with_every_detail_and_schedules_this_topic(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    llm.call(
        "topic_defer",
        done_when="it happened",
        waiting_for="Reply from Sam about Saturday",
        prompt="Search the email for a reply from sam@example.com about Saturday.",
        every_minutes=15,
    )
    llm.answer("I'll watch for Sam's reply.")
    await agent.inbound(fe, "tell me when Sam answers")
    card = await chat.next()
    assert card.card["kind"] == "task" and card.card["icon"] == "⏳"
    assert card.card["buttons"] == ["Start", "Edit", "Don't"]
    assert [(f["label"], f["value"]) for f in card.card["fields"]] == [
        ("Waiting for", "Reply from Sam about Saturday"),
        ("How to check", "Search the email for a reply from sam@example.com about Saturday."),
        (background.DONE_WHEN, "it happened"),
        (background.CHECKS, "15"),
    ]
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    n = chat.sent[-1].topic
    entry = topics.get(fe, n)
    assert entry["state"] == "scheduled" and entry["schedule"]["every"] == 15
    assert entry["schedule"]["next"] > time.time() + 14 * 60
    assert [e["n"] for e in topics.all_topics(fe)] == [n]  # no second topic: this one waits


async def test_a_declined_or_too_frequent_deferral_schedules_nothing(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    llm.call("topic_defer", done_when="it happened", waiting_for="X", prompt="Check x", every_minutes=1)
    llm.answer("OK.")
    await agent.inbound(fe, "watch x every minute")
    await settle(agent)
    assert "at most every 5 minutes" in str(llm.requests[-1]["messages"][-1])
    llm.call("topic_defer", done_when="it happened", waiting_for="X", prompt="Check x", every_minutes=5)
    llm.answer("OK.")
    await agent.inbound(fe, "every 5 then")
    await approve_next_card(agent, chat, fe, "decline")
    await settle(agent)
    assert not [e for e in topics.all_topics(fe) if e.get("schedule")]


async def test_wakeups_wait_for_idle_and_a_quiet_one_leaves_only_its_action_lines(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    n = waiting_topic(fe)
    idle(agent, IDLE - 10)  # someone wrote 4 minutes 50 ago
    await runner.tick()
    assert not agent.busy() and not llm.requests

    idle(agent)
    since = agent.last_input
    turns_before = len(fe.load_context()["turns"])
    llm.call("current_time")
    llm.call("check_result", outcome="waiting", message="no reply from Sam yet")
    await runner.tick()
    await settle(agent)
    sent = llm.requests[0]["messages"]
    assert "[This topic woke up on its own, check 1" in sent[-1]["content"]
    assert "Tell me when Sam answers about Saturday" in str(sent)  # the topic's own turns are in view
    assert agent.last_input == since  # a wakeup is no input
    assert not chat.sent and True not in chat.typed  # no message, no "working…"
    assert chat.woke == [True, False]  # only its topic's line says it works
    assert chat.acts and chat.details == 0  # only its ✓ lines show
    s = topics.get(fe, n)["schedule"]
    assert s["last"] == "no reply from Sam yet" and s["checks"] == 1
    assert s["next"] == pytest.approx(time.time() + 15 * 60, abs=5)
    assert len(fe.load_context()["turns"]) == turns_before  # the topic's turns don't grow

    await runner.tick()  # not due yet: nothing runs, however idle
    assert len(llm.requests) == 2


async def test_news_goes_into_the_topic_and_it_goes_on(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    n = waiting_topic(fe)
    idle(agent)
    llm.call("check_result", outcome="notify", message="Sam wrote: maybe Sunday instead.")
    await runner.tick()
    await settle(agent)
    assert chat.sent[-1].text == "Sam wrote: maybe Sunday instead." and chat.sent[-1].topic == n
    assert topics.get(fe, n)["state"] == "scheduled" and topics.get(fe, n)["schedule"]["checks"] == 1
    assert fe.load_context()["turns"][-1]["content"] == "Sam wrote: maybe Sunday instead."


async def test_a_question_waits_for_the_person_and_their_answer_reaches_the_next_wakeup(
    runner, agent, llm, chat, accounts
):
    """The gap this design closes (SPEC §4.1): before, a check never saw the person's answer to its note."""
    fe = person(accounts)
    n = waiting_topic(fe)
    idle(agent)
    llm.call("check_result", outcome="needs_you", message="Sam offered 17:00 Sunday. OK?")
    await runner.tick()
    await settle(agent)
    entry = topics.get(fe, n)
    assert entry["state"] == "waiting" and entry["question"] == "Sam offered 17:00 Sunday. OK?"
    assert entry["schedule"]["paused"] is True
    idle(agent)
    await runner.tick()  # paused while it waits on the person
    assert len(llm.requests) == 1

    llm.answer("Got it: 17:00 Sunday works, and any time Sunday is fine.")
    await agent.inbound(fe, "17:00 is fine, and any time Sunday works", topic=n)
    await settle(agent)
    entry = topics.get(fe, n)
    assert entry["state"] == "scheduled" and not entry["schedule"].get("paused") and "question" not in entry

    topics.update(fe, n, lambda e: {**e, "schedule": {**e["schedule"], "next": time.time() - 1}})
    idle(agent)
    llm.call("check_result", outcome="waiting", message="sent Sam the yes, waiting for his confirmation")
    await runner.tick()
    await settle(agent)
    assert "any time Sunday works" in str(llm.requests[-1]["messages"])  # the person's answer is in view


async def test_done_ends_the_schedule_and_the_topic_says_so(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    n = waiting_topic(fe)
    idle(agent)
    llm.call("check_result", outcome="done", message="Sam confirmed Saturday at 7.")
    await runner.tick()
    await settle(agent)
    assert chat.sent[-1].text == "Sam confirmed Saturday at 7." and chat.sent[-1].topic == n
    entry = topics.get(fe, n)
    assert entry["state"] == "done" and "schedule" not in entry


async def test_a_never_ending_topic_isnt_closed(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    n = waiting_topic(fe, done_when=background.NEVER)
    idle(agent)
    llm.call("check_result", outcome="done", message="A new email from Sam.")
    await runner.tick()
    await settle(agent)
    assert topics.get(fe, n)["state"] == "scheduled" and topics.get(fe, n)["schedule"]["checks"] == 1
    assert background.NEVER_DONE in llm.requests[0]["messages"][-1]["content"]


async def test_a_message_stops_a_running_wakeup_and_gets_the_model(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    n = waiting_topic(fe)
    idle(agent)
    gate = asyncio.Event()
    real = llm.chat

    async def slow(messages, *a, **kw):
        if "woke up on its own" in str(messages[-1].get("content")):
            await gate.wait()
        return await real(messages, *a, **kw)

    llm.chat = slow
    await runner.tick()
    await asyncio.sleep(0.05)
    assert agent.busy() and not agent.running  # a wakeup runs; no person's request does
    llm.answer("Hello!")
    await agent.inbound(fe, "hi")
    await settle(agent)
    assert chat.texts() == ["Hello!"]
    s = topics.get(fe, n)["schedule"]
    assert s["next"] <= time.time() and s["checks"] == 0  # it runs again at the next idle time
    assert not agent.idle()


async def test_one_wakeup_at_a_time_the_most_overdue_first(runner, agent, llm, accounts):
    fe, hb = person(accounts), accounts.create("Hb")
    waiting_topic(fe, next=time.time() - 60)
    waiting_topic(hb, "Parcel", title="Parcel", next=time.time() - 600)
    idle(agent)
    llm.call("check_result", outcome="waiting", message="nothing yet")
    await runner.tick()
    await runner.tick()  # the first still runs, or just ended: never two at once
    await settle(agent)
    assert "Waiting for: Parcel" in llm.requests[0]["messages"][-1]["content"]


async def test_the_persons_run_now_edit_and_stop(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    n = waiting_topic(fe, next=time.time() + 3600)
    assert actions.edit_topic(fe, n, {"prompt": "Check the inbox for Sam", "every": 3600}, via="topic") == ""
    s = topics.get(fe, n)["schedule"]
    assert s["prompt"] == "Check the inbox for Sam" and s["every"] == 60
    assert actions.edit_topic(fe, n, {"every": 60}, via="topic") == "check at most every 5 minutes"
    planned = topics.get(fe, n)["schedule"]["next"]
    assert actions.run_topic_now(fe, n, via="topic")
    agent.last_input = time.time()  # someone is using sha: Run now doesn't wait for idle
    llm.call("check_result", outcome="waiting", message="nothing yet")
    await runner.tick()
    await settle(agent)
    assert len(llm.requests) == 1 and topics.get(fe, n)["schedule"]["next"] == planned  # it keeps its time
    assert actions.stop_topic(fe, n, via="topic")
    assert "schedule" not in topics.get(fe, n) and topics.get(fe, n)["state"] == ""
    assert not actions.stop_topic(fe, n, via="topic")


async def test_something_at_a_time_of_day_wakes_then_every_day(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    llm.call(
        "topic_defer", done_when=background.NEVER, waiting_for="Morning summary", prompt="Summarise email", at=["08:00"]
    )
    llm.answer("Every morning at 08:00.")
    await agent.inbound(fe, "summarise my email every morning")
    await approve_next_card(agent, chat, fe)
    await settle(agent)
    [entry] = [e for e in topics.all_topics(fe) if e.get("schedule")]
    s = entry["schedule"]
    assert s["at"] == ["08:00"] and s["every"] == 1440
    when = dt.datetime.fromtimestamp(s["next"], LONDON)
    assert (when.hour, when.minute) == (8, 0)


@pytest.mark.made_up_rule
async def test_a_deferral_never_keeps_an_address_the_model_made_up(runner, agent, llm, chat, accounts):
    """SPEC §5.2 "Made-up addresses": an address in a deferral's instructions must be one the request was shown or
    the person sent; its wakeups may open only those (seen: a tracking address from memory, a 404, it ended)."""
    fe = person(accounts)
    guessed = "https://www.fedex.com/en-gb/tracking?track-numbers=385413566727"
    sent = "https://parcel.example/t/AB123"
    for url in (guessed, sent):
        llm.call(
            "topic_defer",
            done_when="it happened",
            waiting_for="Parcel",
            prompt=f"Open {url} and read the status.",
            every_minutes=60,
        )
    llm.answer("Watching it.")
    await agent.inbound(fe, f"tell me when {sent} says delivered")
    await approve_next_card(agent, chat, fe)
    await settle(agent)
    refused = [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"][-2]
    assert refused.startswith(f"Not deferred: {guessed} isn't in anything you read")
    [entry] = [e for e in topics.all_topics(fe) if e.get("schedule")]
    assert entry["schedule"]["links"] == [sent] and background.links_of(fe, entry["n"]) == [sent]


async def test_always_allow_in_a_topic_is_kept_for_its_later_runs(runner, agent, llm, chat, accounts):
    """SPEC §4.1, §5.3: "Always allow for this topic" is kept on the topic, for every later run in it."""
    fe = person(accounts)
    n = waiting_topic(fe)
    guard.remember(SimpleNamespace(account=fe, task=TaskState(topic=n)), "email:sam@example.com")
    assert topics.get(fe, n)["allowed"] == ["email:sam@example.com"]
    seen: dict = {}
    real = agent.solve

    async def spy(account, text, quiet=False, job=None, links=None, record=None, report=None, topic=0):
        seen["topic"] = topic
        report.update(outcome="waiting", message="nothing")
        return "", text

    agent.solve = spy
    idle(agent)
    await runner.tick()
    await settle(agent)
    agent.solve = real
    assert seen["topic"] == n and topics.get(fe, n)["allowed"] == ["email:sam@example.com"]


async def test_check_result_is_only_for_a_wakeup(agent, llm, chat):
    result = await background.check_result(
        {"outcome": "done", "message": "x"}, SimpleNamespace(task=TaskState(), account=None)
    )
    assert result.error


async def test_an_answer_without_check_result_is_asked_again_and_never_closes_it(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    n = waiting_topic(fe)
    idle(agent)
    llm.answer("Sam replied!")
    llm.answer("still nothing")
    await runner.tick()
    await settle(agent)
    assert background.REPORT_RETRY in str(llm.requests[-1]["messages"])
    entry = topics.get(fe, n)
    assert entry["state"] == "scheduled" and entry["schedule"]["last_how"] == "ended_early"
    assert not chat.sent


def test_the_active_topics_are_listed_for_the_model(accounts):
    fe = person(accounts)
    n = waiting_topic(fe)
    other = topics.new(fe, title="Weather")
    block = context.context_block(fe, "Europe/London", "Home")
    assert f"topic {n}: Saturday with Sam (checking on its own)" in block
    assert f"topic {other}:" not in block
