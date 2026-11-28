# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""A topic's title, memory and whether it waits for the person (SPEC §4.1, §7.4): asked at the end of each
request; which topic a message goes to is the person's choice, never the model's."""

from __future__ import annotations

from aha_local import folding, messages, topics

from .conftest import person, settle


def test_a_clean_title():
    assert folding.clean('"Waitrose order and Evri return."') == "Waitrose order and Evri return"
    assert folding.clean("Title: Lisbon trip\nmore") == "Lisbon trip"
    assert folding.clean("This is far too long to be a title for anything at all really") == ""
    assert folding.clean("[sha app]") == "" and folding.clean("") == ""


def test_the_reply_gives_title_waiting_and_memory():
    T = folding.Topic
    assert folding.parse("TITLE: Evri parcel tracking") == T("Evri parcel tracking", "", False)
    assert folding.parse("**TITLE** - Flat Iron booking.") == T("Flat Iron booking", "", False)
    assert folding.parse("Lisbon trip") == T("Lisbon trip", "", False)
    assert folding.parse("TITLE: Evri parcel\nWAITING: no\nMEMORY:\n- Tracking H01\n- Collect 26/11/2026") == T(
        "Evri parcel", "- Tracking H01\n- Collect 26/11/2026", False
    )
    assert folding.parse("TITLE: Trip\n**WAITING:** Yes\n**MEMORY:** none") == T("Trip", "", True)


def test_the_question_quotes_the_message_and_never_asks_about_new_subjects():
    asked = folding.ask("ok, let's change topics.")
    assert "“ok, let's change topics.”" in asked and "TITLE:" in asked and "NEW" not in asked


async def test_a_question_waits_for_the_person_and_the_title_is_asked_at_the_end_of_the_cached_prompt(
    agent, llm, chat, accounts
):
    fe = person(accounts)
    llm.topic_reply = "TITLE: Flat Iron booking\nWAITING: yes\nMEMORY: none"
    llm.answer("There are two tables free tonight. Which one would you like?")
    await agent.inbound(fe, "book Flat Iron Friday")
    await settle(agent)
    n = chat.sent[-1].topic
    entry = topics.get(fe, n)
    assert entry["title"] == "Flat Iron booking" and entry["state"] == "waiting"
    assert entry["question"] == "There are two tables free tonight. Which one would you like?"
    asked = llm.titles[0]
    assert asked[: len(llm.requests[-1]["messages"])] == llm.requests[-1]["messages"]  # same prefix: cached
    assert asked[-2] == {"role": "assistant", "content": "There are two tables free tonight. Which one would you like?"}
    assert "Reply with one line only" not in str(fe.load_context())  # never part of the conversation
    # The person answers in the topic: it no longer waits on them.
    llm.topic_reply = "TITLE: Flat Iron booking\nWAITING: no\nMEMORY: none"
    llm.answer("Booked the window table.")
    await agent.inbound(fe, "the window one", topic=n)
    await settle(agent)
    assert topics.get(fe, n)["state"] == "" and "question" not in topics.get(fe, n)
    assert chat.sent[-1].topic == n


async def test_a_task_that_stopped_stays_open_and_isnt_titled(agent, llm, chat, accounts, monkeypatch):
    fe = person(accounts)

    async def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent, "solve", broken)
    await agent.inbound(fe, "do the thing")
    await settle(agent)
    assert chat.sent[-1].text == messages.FAILED and chat.sent[-1].open == "stopped"
    assert topics.get(fe, chat.sent[-1].topic)["title"] == ""


async def test_each_topic_sees_only_its_own_turns(agent, llm, chat, accounts):
    """A request sees its own topic only (SPEC §4.1 "Context"): another topic's messages aren't in view."""
    fe = person(accounts)
    llm.answer("The parcel is at the depot.")
    await agent.inbound(fe, "where is my evri parcel")
    await settle(agent)
    parcel = chat.sent[-1].topic
    llm.answer("Sunny, 24°C.")
    await agent.inbound(fe, "weather in Lisbon?")
    await settle(agent)
    lisbon = chat.sent[-1].topic
    assert lisbon != parcel
    llm.answer("It should arrive tomorrow.")
    await agent.inbound(fe, "and when will it arrive?", topic=parcel)
    await settle(agent)
    seen = str(llm.requests[-1]["messages"])
    assert "evri parcel" in seen and "Lisbon" not in seen
