# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Folding after a break (SPEC §7.4 "Folding"): where the current part starts, its title, what stays open."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from aha_local import folding, messages

from .conftest import person, settle

LONDON = ZoneInfo("Europe/London")


def at(day: int, hour: int, minute: int = 0) -> int:
    return int(dt.datetime(2026, 11, day, hour, minute, tzinfo=LONDON).timestamp())


def items(*stamps, t="msg"):
    return [{"kind": "item", "item": {"seq": i + 1, "ts": ts, "t": t}} for i, ts in enumerate(stamps)]


def test_a_part_starts_after_two_hours_or_a_new_day():
    assert folding.segment_start(items(at(24, 9), at(24, 9, 30), at(24, 10)), LONDON) == (1, at(24, 9))
    assert folding.segment_start(items(at(24, 9), at(24, 11, 30), at(24, 12)), LONDON) == (2, at(24, 11, 30))
    assert folding.segment_start(items(at(23, 23, 50), at(24, 0, 10)), LONDON) == (2, at(24, 0, 10))  # new day
    states = [*items(at(24, 9)), {"kind": "item", "item": {"seq": 2, "ts": at(24, 15), "t": "approval_state"}}]
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
    assert "give a title" in asked[-1]["content"]
    assert "give a title" not in str(fe.load_context())  # never part of the conversation


async def test_a_task_that_stopped_stays_open_and_gets_no_title(agent, llm, chat, accounts, monkeypatch):
    fe = person(accounts)

    async def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent, "solve", broken)
    await agent.inbound(fe, "do the thing")
    await settle(agent)
    assert chat.sent[-1].text == messages.FAILED and chat.sent[-1].open == "stopped"
    assert chat.titles == []
