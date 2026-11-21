# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Reminders and scheduled tasks (SPEC §3.4 "Reminders and scheduled tasks", test 19)."""

from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

import pytest

from aha_local.tools import schedule
from aha_local.tools.schedule import Scheduler, next_time

from .conftest import person, settle

LONDON = ZoneInfo("Europe/London")


def local(text: str) -> float:
    return dt.datetime.fromisoformat(text).replace(tzinfo=LONDON).timestamp()


def entry(anchor: str, repeat: str, kind: str = "reminder", text: str = "Call Sam") -> dict:
    return {"id": "abc123", "kind": kind, "text": text, "repeat": repeat, "anchor": anchor, "next": local(anchor)}


@pytest.mark.parametrize(
    ("anchor", "repeat", "after", "expected"),
    [
        # Daily 09:00 across the spring DST change: still 09:00 local, one hour earlier in UTC.
        ("2027-03-27T09:00", "daily", "2027-03-27T09:00", "2027-03-28T09:00"),
        ("2027-10-30T09:00", "daily", "2027-10-30T09:00", "2027-10-31T09:00"),
        ("2026-11-20T08:30", "weekdays", "2026-11-20T08:30", "2026-11-23T08:30"),  # Friday → Monday
        ("2026-11-22T10:00", "weekly", "2026-11-22T10:00", "2026-11-29T10:00"),
        ("2027-01-31T12:00", "monthly", "2027-01-31T12:00", "2027-02-28T12:00"),  # short month: its last day
        ("2027-01-31T12:00", "monthly", "2027-02-28T12:00", "2027-03-31T12:00"),
        ("2026-11-22T10:15", "hourly", "2026-11-22T13:20", "2026-11-22T14:15"),
    ],
)
def test_next_times_are_local_and_dst_correct(anchor, repeat, after, expected):
    got = next_time(entry(anchor, repeat), LONDON, local(after))
    assert dt.datetime.fromtimestamp(got, LONDON).replace(tzinfo=None).isoformat(timespec="minutes") == expected


def test_a_one_off_has_no_next_time():
    assert next_time(entry("2026-11-22T10:00", "none"), LONDON, local("2026-11-22T10:00")) is None


@pytest.fixture
def scheduled_agent(agent, registry):
    schedule.register(registry)
    return agent, Scheduler(agent.accounts, agent, "Europe/London")


async def test_a_reminder_needs_no_card_and_reaches_only_its_person(scheduled_agent, llm, chat, accounts):
    agent, scheduler = scheduled_agent
    fe, hb = person(accounts), accounts.create("Hb")
    llm.call("schedule_add", kind="reminder", text="Call Sam about Saturday", in_minutes=30)
    llm.answer("I'll remind you in 30 minutes.")
    await agent.inbound(fe, "remind me in 30 minutes to call Sam about Saturday")
    await settle(agent)
    assert not any(m.card for m in chat.sent)
    (saved,) = schedule.load(fe)
    assert saved["kind"] == "reminder" and not schedule.load(hb)
    assert b"Call Sam" not in (fe.dir / "schedule.json").read_bytes()  # encrypted at rest

    chat.sent.clear()
    await scheduler.tick(now=saved["next"] - 60)
    assert not chat.sent  # not yet
    await scheduler.tick(now=saved["next"] + 1)
    assert [m.text for m in chat.sent] == ["⏰ Call Sam about Saturday"]
    assert any(e.get("text") == "⏰ Call Sam about Saturday" for e in fe.read_transcript() if e["kind"] == "out")
    assert not any("Call Sam" in str(e) for e in hb.read_transcript())
    assert schedule.load(fe) == []  # a one-off is done


async def test_a_scheduled_task_needs_a_card_and_runs_later_as_its_person(scheduled_agent, llm, chat, accounts):
    agent, scheduler = scheduled_agent
    fe = person(accounts)
    llm.call(
        "schedule_add",
        kind="task",
        text="Check if Fictional Band tickets are on sale",
        at="2027-01-05 09:00",
        repeat="daily",
    )
    llm.answer("Scheduled.")
    await agent.inbound(fe, "every morning check if Fictional Band tickets are on sale")
    card = await chat.next()
    assert card.card["kind"] == "schedule"
    assert [(f["label"], f["value"]) for f in card.card["fields"]] == [
        ("What", "Check if Fictional Band tickets are on sale"),
        ("When", "2027-01-05 09:00"),
        ("Repeats", "daily"),
    ]
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    (saved,) = schedule.load(fe)
    assert saved["next"] == local("2027-01-05T09:00")

    # Day one: nothing new. Nothing is sent, and the turn isn't kept.
    chat.sent.clear()
    turns_before = len(fe.load_context().get("turns", []))
    llm.answer("NOTHING_TO_REPORT")
    await scheduler.tick(now=saved["next"] + 5)
    await settle(agent)
    assert "Scheduled task the user set up" in llm.requests[-1]["messages"][-1]["content"]
    assert not chat.sent and len(fe.load_context().get("turns", [])) == turns_before
    assert schedule.load(fe)[0]["next"] == local("2027-01-06T09:00")

    # Day two: news.
    llm.answer("Tickets for Fictional Band are on sale now.")
    await scheduler.tick(now=local("2027-01-06T09:00") + 5)
    await settle(agent)
    assert [m.text for m in chat.sent] == ["⏰ Tickets for Fictional Band are on sale now."]


async def test_a_declined_task_schedules_nothing(scheduled_agent, llm, chat, accounts):
    agent, _ = scheduled_agent
    fe = person(accounts)
    llm.call("schedule_add", kind="task", text="Email Sam every day", in_minutes=60, repeat="daily")
    llm.answer("OK.")
    await agent.inbound(fe, "do it")
    card = await chat.next()
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)
    assert schedule.load(fe) == []


async def test_a_task_waits_for_a_running_one(scheduled_agent, accounts):
    agent, scheduler = scheduled_agent
    fe = person(accounts)
    schedule.save(fe, [entry("2026-11-22T10:00", "none", kind="task")])
    agent.running[fe.address] = object()  # something is running for this person
    await scheduler.tick(now=local("2026-11-22T10:00") + 1)
    assert len(schedule.load(fe)) == 1  # still waiting for its turn
    del agent.running[fe.address]


async def test_missed_while_off(scheduled_agent, chat, accounts):
    _, scheduler = scheduled_agent
    fe = person(accounts)
    one_off = entry("2026-11-22T08:00", "none")
    daily = {**entry("2026-11-22T07:00", "daily", text="Take vitamins"), "id": "def456"}
    schedule.save(fe, [one_off, daily])
    await scheduler.tick(now=local("2026-11-22T23:00"))  # 15 and 16 hours late
    assert [m.text for m in chat.sent] == [
        "⏰ Missed while the computer was off (reminder, Sun 22 Nov 2026, 08:00): Call Sam"
    ]
    (left,) = schedule.load(fe)
    assert left["id"] == "def456" and left["next"] == local("2026-11-23T07:00")

    chat.sent.clear()
    schedule.save(fe, [entry("2026-11-23T08:00", "none")])
    await scheduler.tick(now=local("2026-11-23T10:00"))  # 2 hours late: still sent, marked
    assert chat.sent[0].text.startswith("⏰ Call Sam _(late: due Mon 23 Nov 2026, 08:00)_")


async def test_limits_and_past_times(scheduled_agent, accounts, config):
    from aha_local.tools.registry import TaskState, ToolContext

    agent, _ = scheduled_agent
    fe = person(accounts)
    ctx = ToolContext(fe, agent.approvals, config, TaskState())
    past = await schedule.schedule_add({"kind": "reminder", "text": "x", "at": "2020-01-01 09:00"}, ctx)
    assert past.error and "already passed" in past.text
    hourly = [{**entry("2027-01-01T09:00", "hourly", kind="task"), "id": f"h{i}"} for i in range(5)]
    schedule.save(fe, hourly)
    sixth = await schedule.schedule_add({"kind": "task", "text": "check", "in_minutes": 60, "repeat": "hourly"}, ctx)
    assert sixth.error and "at most 5 hourly" in sixth.text
    schedule.save(fe, [{**entry("2027-01-01T09:00", "none"), "id": f"r{i}"} for i in range(50)])
    full = await schedule.schedule_add({"kind": "reminder", "text": "x", "in_minutes": 5}, ctx)
    assert full.error and "50" in full.text


async def test_reminders_command_lists_and_cancels(scheduled_agent, chat, accounts):
    agent, _ = scheduled_agent
    fe = person(accounts)
    schedule.save(fe, [{**entry("2027-01-01T09:00", "weekly"), "next": time.time() + 3600}])
    await agent.inbound(fe, "reminders")
    assert "`abc123` Reminder" in chat.sent[-1].text and "repeats weekly" in chat.sent[-1].text
    await agent.inbound(fe, "cancel reminder abc123")
    assert chat.sent[-1].text == "Cancelled." and schedule.load(fe) == []
