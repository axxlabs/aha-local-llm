# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topics (SPEC §7.4 "Folding"): where the topic going on starts, its title, what stays open."""

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


def test_a_topic_starts_at_a_new_subject_or_a_cut_never_at_a_break_in_time():
    assert folding.segment_start(items(at(24, 9), at(24, 11, 30), at(25, 12))) == (1, at(24, 9))
    states = [*items(at(24, 9)), {"seq": 2, "ts": at(24, 15), "t": "approval_state"}]
    assert folding.segment_start(states) == (1, at(24, 9))  # only what the chat shows counts
    assert folding.segment_start([]) is None


def test_a_clean_title():
    assert folding.clean('"Waitrose order and Evri return."') == "Waitrose order and Evri return"
    assert folding.clean("Title: Lisbon trip\nmore") == "Lisbon trip"
    assert folding.clean("This is far too long to be a title for anything at all really") == ""
    assert folding.clean("[sha app]") == "" and folding.clean("") == ""


async def test_a_question_stays_open_and_the_title_is_asked_at_the_end_of_the_cached_prompt(agent, llm, chat, accounts):
    fe = person(accounts)
    fe.transcript({"kind": "item", "item": {"seq": 7, "ts": int(dt.datetime.now().timestamp()), "t": "msg"}})
    llm.topic_reply = "SAME: Flat Iron booking\nWAITING: yes\nMEMORY: none"
    fe.seq = 9  # the answer's chat item (the real channel numbers it as it's sent)
    llm.answer("There are two tables free tonight. Which one would you like?")
    await agent.inbound(fe, "book Flat Iron Friday")
    await settle(agent)
    assert chat.sent[-1].open == ""  # the model says it waits, not a "?" in its words
    assert chat.titles == [(7, "Flat Iron booking")] and chat.waiting == [9]
    asked = llm.titles[0]
    assert asked[: len(llm.requests[-1]["messages"])] == llm.requests[-1]["messages"]  # same prefix: cached
    assert asked[-2] == {"role": "assistant", "content": "There are two tables free tonight. Which one would you like?"}
    assert "First line: NEW:" in asked[-1]["content"] and "WAITING:" in asked[-1]["content"]
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
    T = folding.Topic
    assert folding.parse("NEW: Evri parcel tracking") == T(True, "Evri parcel tracking", "", False)
    assert folding.parse("SAME: Waitrose order and memories") == T(False, "Waitrose order and memories", "", False)
    assert folding.parse("**NEW** - Flat Iron booking.") == T(True, "Flat Iron booking", "", False)
    assert folding.parse("Lisbon trip") == T(False, "Lisbon trip", "", False)
    assert folding.parse("SAME: Evri parcel\nWAITING: no\nMEMORY:\n- Tracking H01\n- Collect 26/11/2026") == T(
        False, "Evri parcel", "- Tracking H01\n- Collect 26/11/2026", False
    )
    assert folding.parse("NEW: Trip\n**WAITING:** Yes\n**MEMORY:** none") == T(True, "Trip", "", True)
    shown = items(at(24, 10), at(24, 10, 5), at(24, 10, 49), at(24, 10, 50))
    topic = {"seq": 9, "ts": at(24, 10, 50), "t": "title", "from": 3, "new": True}
    assert folding.segment_start([*shown, topic]) == (3, at(24, 10, 49))  # from the new subject on


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


def test_the_question_quotes_the_message_and_explains_new():
    assert "“ok, let's change topics.”" in folding.ask("10:24", "ok, let's change topics.")
    assert "two “who is …” questions about different people are two subjects" in folding.ask("10:24", "x")


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
