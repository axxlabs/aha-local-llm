# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Reminders and scheduled tasks, per account (SPEC §3.4 "Reminders and scheduled tasks").

A reminder is a message at a time. A scheduled task is an instruction the agent carries out at a time, as that
account, once or repeating, and its answer is always sent. Watching for something to happen is a background task
(background.py), not this. Times are local to the household's timezone.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import logging
import secrets
import time
from zoneinfo import ZoneInfo

from .. import dates
from ..accounts import Account
from ..approvals import Field
from . import guard
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

log = logging.getLogger("aha.schedule")

FILE = "schedule.json"
REPEATS = ("none", "hourly", "daily", "weekdays", "weekly", "monthly")
KINDS = ("reminder", "task")
MAX_ENTRIES = 50
MAX_HOURLY_TASKS = 5
MAX_TEXT = 500
MAX_AHEAD = 366 * 86400
MAX_LATE = 12 * 3600  # a one-off missed by more than this (computer off) is dropped, with a message
LATE_NOTE = 5 * 60
CHECK_EVERY = 30.0
TASK = (
    "[Scheduled task the user set up earlier{late}; they aren't necessarily looking at the chat]\n{text}\n\nDo it now."
)


# ---- time


def _at(day: dt.date, clock: dt.time, tz: ZoneInfo) -> dt.datetime:
    return dt.datetime.combine(day, clock, tzinfo=tz)


def next_time(entry: dict, tz: ZoneInfo, after: float) -> float | None:
    """The first time strictly after `after` (epoch seconds) this entry is due; None for a one-off."""
    anchor = dates.parse_local(entry["anchor"], tz)
    repeat = entry.get("repeat", "none")
    if anchor is None or repeat == "none":
        return None
    if repeat == "hourly":
        steps = max(0, int((after - anchor.timestamp()) // 3600) + 1)
        return anchor.timestamp() + steps * 3600
    start = max(anchor, dt.datetime.fromtimestamp(after, tz)).date()
    clock = anchor.timetz().replace(tzinfo=None)
    for i in range(0, 400):
        day = start + dt.timedelta(days=i)
        if repeat == "weekdays" and day.weekday() >= 5:
            continue
        if repeat == "weekly" and day.weekday() != anchor.weekday():
            continue
        if repeat == "monthly" and day.day != min(anchor.day, calendar.monthrange(day.year, day.month)[1]):
            continue
        t = _at(day, clock, tz).timestamp()
        if t > after:
            return t
    return None


def describe(entry: dict, tz: ZoneInfo) -> str:
    when = dates.for_model(dt.datetime.fromtimestamp(entry["next"], tz))
    rep = "" if entry.get("repeat", "none") == "none" else f", repeats {entry['repeat']}"
    what = "Reminder" if entry["kind"] == "reminder" else "Task"
    return f"`{entry['id']}` {what}, next {when}{rep}: {entry['text']}"


# ---- storage


def load(account: Account) -> list[dict]:
    return account.vault.read_json(account.dir / FILE, []) or []


def save(account: Account, entries: list[dict]) -> None:
    path = account.dir / FILE
    if entries:
        account.vault.write_json(path, entries)
    else:
        path.unlink(missing_ok=True)


def update(account: Account, entry_id: str, fn) -> None:
    """Change one entry (fn returns the new entry, or None to remove it); read and written with no await between."""
    entries = []
    for e in load(account):
        if e["id"] == entry_id:
            e = fn(e)
            if e is None:
                continue
        entries.append(e)
    save(account, entries)


# ---- tools


def _clean(value, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


def _when(args: dict, tz: ZoneInfo, now: float) -> tuple[dt.datetime | None, str]:
    if args.get("in_minutes") not in (None, ""):
        try:
            minutes = float(args["in_minutes"])
        except (TypeError, ValueError):
            return None, "in_minutes must be a number"
        when = dt.datetime.fromtimestamp(now + minutes * 60, tz).replace(second=0, microsecond=0)
        if minutes * 60 < 30:
            return None, "the time must be at least a minute from now"
    else:
        when = dates.parse_local(args.get("at", ""), tz)
        if when is None:
            return None, "give `at` as a local date and time like 2026-11-22 09:00, or `in_minutes`"
    if when.timestamp() <= now + 20:
        return None, f"{dates.for_model(when)} has already passed: check the current time in the <context> block"
    if when.timestamp() > now + MAX_AHEAD:
        return None, "that's more than a year away"
    return when, ""


def _check_limits(entries: list[dict], kind: str, repeat: str) -> str:
    if len(entries) >= MAX_ENTRIES:
        return f"there are already {MAX_ENTRIES} reminders and scheduled tasks: cancel some first"
    hourly = sum(1 for e in entries if e["kind"] == "task" and e.get("repeat") == "hourly")
    if kind == "task" and repeat == "hourly" and hourly >= MAX_HOURLY_TASKS:
        return f"at most {MAX_HOURLY_TASKS} hourly tasks: make this one less frequent or cancel one"
    return ""


async def schedule_add(args: dict, ctx: ToolContext) -> ToolResult:
    kind = str(args.get("kind", "reminder")).lower()
    repeat = str(args.get("repeat", "none") or "none").lower()
    text = _clean(args.get("text"))
    if kind not in KINDS:
        return ToolResult("kind must be reminder or task.", error=True)
    if repeat not in REPEATS:
        return ToolResult(f"repeat must be one of {', '.join(REPEATS)}.", error=True)
    if not text:
        return ToolResult("Give the text: what to remind them, or what to do.", error=True)
    tz, now = dates.tz(ctx), time.time()
    when, why = _when(args, tz, now)
    if when is None:
        return ToolResult(f"Not scheduled: {why}.", error=True)
    if why := _check_limits(load(ctx.account), kind, repeat):
        return ToolResult(f"Not scheduled: {why}.", error=True)
    if kind == "task":
        # It will act later without the person watching, so they approve it (SPEC §3.4).
        fields = [
            Field("What", text, "longtext"),
            Field("When", dates.editable(when)),
            Field("Repeats", repeat),
        ]
        d, refused = await guard.send(ctx, "schedule", "Schedule this task?", fields)
        if refused:
            return refused
        text, repeat = _clean(d.values[0]), d.values[2].strip().lower()
        when = dates.parse_local(d.values[1], tz)
        if not text or repeat not in REPEATS or when is None or when.timestamp() <= time.time():
            return ToolResult(
                "The edited details weren't valid (a future 'YYYY-MM-DD HH:MM', a known repeat), so "
                "nothing was scheduled. Ask the person.",
                error=True,
            )
    entries = load(ctx.account)
    if why := _check_limits(entries, kind, repeat):
        return ToolResult(f"Not scheduled: {why}.", error=True)
    entry = {
        "id": secrets.token_hex(3),
        "kind": kind,
        "text": text,
        "repeat": repeat,
        "anchor": when.replace(tzinfo=None).isoformat(timespec="minutes"),
        "next": when.timestamp(),
        "created": int(time.time()),
    }
    save(ctx.account, [*entries, entry])
    return ToolResult(f"Scheduled. {describe(entry, tz)}. Tell the person briefly, with the time.")


async def schedule_list(args: dict, ctx: ToolContext) -> ToolResult:
    entries = sorted(load(ctx.account), key=lambda e: e["next"])
    if not entries:
        return ToolResult("No reminders or scheduled tasks.")
    tz = dates.tz(ctx)
    return ToolResult("\n".join(describe(e, tz) for e in entries))


async def schedule_cancel(args: dict, ctx: ToolContext) -> ToolResult:
    entry_id = str(args.get("id", "")).strip().strip("`")
    entries = load(ctx.account)
    if not any(e["id"] == entry_id for e in entries):
        return ToolResult("There's no reminder or task with that id: use schedule_list.", error=True)
    save(ctx.account, [e for e in entries if e["id"] != entry_id])
    return ToolResult("Cancelled.")


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "schedule_add",
            "Set a reminder (a message to the user at a time) or a scheduled task (something you'll do for them "
            "at a time, once or repeating: a daily email summary, a weekly report). `at` is the first time it "
            "happens: for 'every weekday at 8am' asked on a Saturday evening, that's Monday 08:00, not now. "
            "Each run's answer is sent. To watch for something to happen (rain, a price drop, tickets on sale, a "
            "reply), use task_create instead. Work out times from the time in the <context> block.",
            params(
                ["kind", "text"],
                kind={"type": "string", "enum": list(KINDS)},
                text=string("The reminder's words, or the task's instruction, written so it makes sense later"),
                at=string("The first time, local, YYYY-MM-DD HH:MM"),
                in_minutes={"type": "number", "description": "Instead of `at`: this many minutes from now"},
                repeat={"type": "string", "enum": list(REPEATS)},
            ),
            schedule_add,
        )
    )
    reg.add(Tool("schedule_list", "List the user's reminders and scheduled tasks.", params(), schedule_list))
    reg.add(
        Tool(
            "schedule_cancel",
            "Cancel one of the user's reminders or scheduled tasks by its id (from schedule_list).",
            params(["id"], id=string("The id")),
            schedule_cancel,
        )
    )


# ---- a scheduled task's run


def task_prompt(entry: dict, late: bool) -> str:
    return TASK.format(
        text=entry["text"],
        late=", running late: the computer was off" if late else "",
    )


def finish(account: Account, entry_id: str, answer: str, stopped: bool) -> str | None:
    """After a run: the message for the chat, always sent ("" when the person already heard from it)."""
    return f"⏰ {answer}" if answer.strip() else ""


# ---- the scheduler


class Scheduler:
    """Every CHECK_EVERY seconds: send due reminders, start due tasks (SPEC §3.4)."""

    def __init__(self, accounts, agent, timezone: str) -> None:
        self.accounts = accounts
        self.agent = agent
        self.tz = dates.tz(timezone)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(CHECK_EVERY)

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for account in self.accounts.all():
            for entry in [e for e in load(account) if e["next"] <= now]:
                await self._due(account, entry, now)

    async def _due(self, account: Account, entry: dict, now: float) -> None:
        late = now - entry["next"]
        once = entry.get("repeat", "none") == "none"
        if entry["kind"] == "task" and late <= MAX_LATE and self.agent.is_running(account):
            return  # its turn comes at the next check
        when = dates.for_model(dt.datetime.fromtimestamp(entry["next"], self.tz))
        if late > MAX_LATE:
            if once:
                what = "reminder" if entry["kind"] == "reminder" else "scheduled task"
                text = f"⏰ Missed while the computer was off ({what}, {when}): {entry['text']}"
                await self.agent.send(account, text)
        elif entry["kind"] == "reminder":
            note = f" _(late: due {when})_" if late > LATE_NOTE else ""
            await self.agent.send(account, f"⏰ {entry['text']}{note}")
        else:
            self.agent.start_scheduled(account, entry, late=late > LATE_NOTE)
        following = None if once else next_time(entry, self.tz, now)
        update(account, entry["id"], lambda e: None if following is None else {**e, "next": following})
