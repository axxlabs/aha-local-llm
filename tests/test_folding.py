# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Folding after a break (SPEC §7.4 "Folding"): where the current part starts, its title, what stays open."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from aha_local import folding, messages
from aha_local.llm import Reply

from .conftest import person, settle

LONDON = ZoneInfo("Europe/London")


def at(day: int, hour: int, minute: int = 0) -> int:
    return int(dt.datetime(2026, 11, day, hour, minute, tzinfo=LONDON).timestamp())


def items(*stamps, t="msg"):
    return [{"seq": i + 1, "ts": ts, "t": t} for i, ts in enumerate(stamps)]


def test_a_part_starts_after_two_hours_or_a_new_day():
    assert folding.segment_start(items(at(24, 9), at(24, 9, 30), at(24, 10)), LONDON) == (1, at(24, 9))
    assert folding.segment_start(items(at(24, 9), at(24, 11, 30), at(24, 12)), LONDON) == (2, at(24, 11, 30))
    assert folding.segment_start(items(at(23, 23, 50), at(24, 0, 10)), LONDON) == (2, at(24, 0, 10))  # new day
    states = [*items(at(24, 9)), {"seq": 2, "ts": at(24, 15), "t": "approval_state"}]
    assert folding.segment_start(states, LONDON) == (1, at(24, 9))  # only what the chat shows counts
    assert folding.segment_start([], LONDON) is None


def test_what_counts_as_asking_and_a_clean_title():
    assert folding.asks("I found two slots: 19:00 and 20:30. Which one would you like?")
    assert folding.asks("Booked?\n\nShall I add it to your calendar?")
    assert not folding.asks("Is it open on Sundays? Yes, 10:00 to 16:00.\n\nIt's on the High St.")
    assert folding.clean('"Waitrose order and Evri return."') == "Waitrose order and Evri return"
    assert folding.clean("Title: Lisbon trip\nmore") == "Lisbon trip"
    assert folding.clean("This is far too long to be a title for anything at all really") == ""
    assert folding.clean("[Aha! app]") == "" and folding.clean("") == ""


async def test_a_question_stays_open_and_the_title_is_asked_at_the_end_of_the_cached_prompt(agent, llm, chat, accounts):
    fe = person(accounts)
    fe.transcript({"kind": "item", "item": {"seq": 7, "ts": int(dt.datetime.now().timestamp()), "t": "msg"}})
    llm.answer("There are two tables free tonight. Which one would you like?")
    await agent.inbound(fe, "book Flat Iron Friday")
    await settle(agent)
    assert chat.sent[-1].open == "asks"
    assert chat.titles == [(7, "Test title")]
    asked = llm.titles[0]
    assert asked[: len(llm.requests[-1]["messages"])] == llm.requests[-1]["messages"]  # same prefix: cached
    assert asked[-2] == {"role": "assistant", "content": "There are two tables free tonight. Which one would you like?"}
    assert "Reply with one line only: NEW:" in asked[-1]["content"]
    assert "Reply with one line only" not in str(fe.load_context())  # never part of the conversation


async def test_a_task_that_stopped_stays_open_and_gets_no_title(agent, llm, chat, accounts, monkeypatch):
    fe = person(accounts)

    async def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent, "solve", broken)
    await agent.inbound(fe, "do the thing")
    await settle(agent)
    assert chat.sent[-1].text == messages.FAILED and chat.sent[-1].open == "stopped"
    assert chat.titles == []


def test_a_new_subject_starts_a_part():
    assert folding.parse("NEW: Evri parcel tracking") == (True, "Evri parcel tracking")
    assert folding.parse("SAME: Waitrose order and memories") == (False, "Waitrose order and memories")
    assert folding.parse("**NEW** - Flat Iron booking.") == (True, "Flat Iron booking")
    assert folding.parse("Lisbon trip") == (False, "Lisbon trip")
    shown = items(at(24, 10), at(24, 10, 5), at(24, 10, 49), at(24, 10, 50))
    topic = {"seq": 9, "ts": at(24, 10, 50), "t": "title", "from": 3, "new": True}
    assert folding.segment_start([*shown, topic], LONDON) == (3, at(24, 10, 49))  # from the new subject on


async def test_when_the_model_says_new_the_part_starts_at_the_persons_message(agent, llm, chat, accounts):
    fe = person(accounts)
    now = int(dt.datetime.now().timestamp())
    fe.transcript({"kind": "item", "item": {"seq": 7, "ts": now - 600, "t": "msg"}})
    fe.seq = 8  # the person's new message was item 8
    fe.transcript({"kind": "item", "item": {"seq": 8, "ts": now, "t": "msg"}})

    async def titled(messages, tools=None, max_tokens=4096, who=None, tool_choice="auto", thinking=True):
        if tool_choice == "none":
            return Reply("NEW: Email search")
        return await plain(messages, tools, max_tokens, who)

    plain = llm.chat
    llm.chat = titled
    llm.answer("Nothing new in your inbox.")
    await agent.inbound(fe, "any new emails?")
    await settle(agent)
    assert chat.titles == [(8, "Email search", "new")]


def test_saying_so_starts_a_new_subject():
    for said in (
        "ok, let's change topics. What happened to Jensen Huang?",
        "Different subject: is the dentist open Saturday?",
        "something else: book Flat Iron",
        "mudando de assunto, quando é o feriado?",
        "changing topics: Who is the president of China",
        "new topic:who is felipe lavratti",
        "Switching subjects - weather tomorrow?",
        "novo assunto: feriados",
        "moving on, what's the time in Tokyo?",
    ):
        assert folding.says_new(said), said
    for same in ("what's on my calendar?", "change the time to 8pm", "another table please"):
        assert not folding.says_new(same), same
    assert "“ok, let's change topics.”" in folding.ask("10:24", "ok, let's change topics.")
    assert "two “who is …” questions about different people are two subjects" in folding.ask("10:24", "x")


async def test_a_said_change_of_subject_splits_even_when_the_model_says_same(agent, llm, chat, accounts):
    fe = person(accounts)
    now = int(dt.datetime.now().timestamp())
    fe.transcript({"kind": "item", "item": {"seq": 7, "ts": now - 600, "t": "msg"}})
    fe.seq = 8
    fe.transcript({"kind": "item", "item": {"seq": 8, "ts": now, "t": "msg"}})
    llm.answer("He's Nvidia's chief executive.")
    await agent.inbound(fe, "ok, let's change topics. What happened to Jensen Huang?")
    await settle(agent)
    assert chat.titles == [(8, "Test title", "new")]
    assert "let's change topics" in llm.titles[0][-1]["content"]  # the question quotes the message


def test_the_current_part_is_found_without_reading_the_whole_history(accounts, monkeypatch):
    """Review: every task read the whole transcript to find where the part starts; now only the first time."""
    fe = person(accounts)
    fe.transcript({"kind": "item", "item": {"seq": 1, "ts": at(24, 9), "t": "msg", "text": "hi"}})
    assert [i["seq"] for i in fe.recent_items()] == [1]
    reads = []
    monkeypatch.setattr(type(fe), "read_transcript", lambda self: reads.append(1) or [])
    fe.transcript({"kind": "item", "item": {"seq": 2, "ts": at(24, 9, 5), "t": "msg", "text": "secret"}})
    fe.transcript({"kind": "tool", "name": "web_search"})
    assert fe.recent_items() == [{"seq": 1, "ts": at(24, 9), "t": "msg"}, {"seq": 2, "ts": at(24, 9, 5), "t": "msg"}]
    assert reads == []  # kept up to date as items were written, and no text kept
    fe.clear_history()
    assert fe.recent_items() == [] and reads == [1]  # read again after the history changed
