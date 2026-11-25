# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Background tasks, per account (SPEC §3.4 "Background tasks").

What the model waits for and checks on its own: a reply, a delivery, a page changing. Each task has the prompt a
check runs, how often to check (at least every 5 minutes) and until when (or never). Checks run only when the
household is idle (nobody typed or answered a card for 5 minutes, no request running), one at a time, and say
nothing until one closes the task: DONE or FAILED sends its message and ends it; WAITING keeps a note for next time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import secrets
import time
from zoneinfo import ZoneInfo

from .. import dates
from ..accounts import Account
from ..approvals import Field
from . import guard
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

log = logging.getLogger("aha.background")

FILE = "tasks.json"
MAX_TASKS = 20
MIN_EVERY = 5  # minutes
MAX_EVERY = 7 * 24 * 60
MAX_AHEAD = 366 * 86400
MAX_TITLE = 120
MAX_PROMPT = 2000
MAX_NOTE = 500
IDLE_AFTER = 5 * 60  # seconds without input from anyone before a check runs
CHECK_EVERY = 30.0
NEVER = "never"
OUTCOME = re.compile(r"^[\W_]*(WAITING|DONE|FAILED)\b[ \t*_`]*[:\-–—]?[ \t*_`]*", re.I)

CHECK = (
    "[Background task you set up earlier, check {n}{late}; the user isn't looking at the chat]\n"
    "Waiting for: {title}\n\n{prompt}\n\n{last}"
    "Check now. Don't message the user while you check. Start your answer with one of:\n"
    "WAITING: one line on what you found, when it hasn't happened yet. Nothing is sent; you'll check again in "
    "{every}.\n"
    "DONE: the message for the user, when it has happened (and what was to be done then is done).\n"
    "FAILED: the message for the user, when it can't happen any more or can't be checked.\n"
    "DONE and FAILED end the task, and the message goes to the chat."
)
LAST_TIME = "Last time you found: {note}\n\n"
CLOSED = {"done": "Task done: {title}", "failed": "Task failed: {title}"}
EXPIRED = "_Task expired: {title}_\n\nI stopped waiting on {when}.{last}"


# ---- storage


def load(account: Account) -> list[dict]:
    return account.vault.read_json(account.dir / FILE, []) or []


def save(account: Account, entries: list[dict]) -> None:
    path = account.dir / FILE
    if entries:
        account.vault.write_json(path, entries)
    else:
        path.unlink(missing_ok=True)


def update(account: Account, task_id: str, fn) -> dict | None:
    """Change one task (fn returns the new task, or None to end it), with no await between read and write.
    Returns the task as it was, or None if there's no such task."""
    entries, found = [], None
    for e in load(account):
        if e["id"] == task_id:
            found, e = e, fn(e)
            if e is None:
                continue
        entries.append(e)
    save(account, entries)
    return found


def every_words(minutes: int) -> str:
    if minutes % 1440 == 0:
        return "1 day" if minutes == 1440 else f"{minutes // 1440} days"
    if minutes % 60 == 0:
        return "1 hour" if minutes == 60 else f"{minutes // 60} hours"
    return f"{minutes} minutes"


def describe(entry: dict, tz: ZoneInfo) -> str:
    until = entry.get("until")
    end = "no end" if until is None else "until " + dates.for_model(dt.datetime.fromtimestamp(until, tz))
    nxt = dates.for_model(dt.datetime.fromtimestamp(entry["next"], tz))
    last = f" Last found: {entry['last']}" if entry.get("last") else ""
    return (
        f"`{entry['id']}` {entry['title']}: every {every_words(entry['every'])}, {end}, next check {nxt}, "
        f"{entry.get('checks', 0)} checks so far.{last}"
    )


# ---- tools


def _clean(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _prompt(value) -> str:
    return "\n".join(" ".join(line.split()) for line in str(value or "").strip().splitlines())[:MAX_PROMPT]


def _every(value) -> tuple[int | None, str]:
    try:
        minutes = round(float(value))
    except (TypeError, ValueError):
        return None, "every_minutes must be a number of minutes"
    if minutes < MIN_EVERY:
        return None, f"check at most every {MIN_EVERY} minutes"
    if minutes > MAX_EVERY:
        return None, "check at least once a week"
    return minutes, ""


def _until(value, tz: ZoneInfo, now: float) -> tuple[float | None, str]:
    """(epoch, "") or (None, "") for never; ("", why) when it isn't valid."""
    text = str(value or "").strip()
    if text.lower() in (NEVER, "none", "no end", ""):
        return None, ""
    when = dates.parse_local(text, tz)
    if when is None:
        return None, f"give `until` as a local date and time like 2026-11-30 18:00, or '{NEVER}'"
    if when.timestamp() <= now + 60:
        return None, f"{dates.for_model(when)} has already passed: check the current time in the <context> block"
    if when.timestamp() > now + MAX_AHEAD:
        return None, f"that's more than a year away: give an earlier time, or '{NEVER}'"
    return when.timestamp(), ""


def _until_text(until: float | None, tz: ZoneInfo) -> str:
    return NEVER if until is None else dates.editable(dt.datetime.fromtimestamp(until, tz))


def _validate(title, prompt, every, until, tz: ZoneInfo, now: float):
    title, prompt = _clean(title, MAX_TITLE), _prompt(prompt)
    if not title:
        return None, "give a short title: what you're waiting for"
    if not prompt:
        return None, "give the prompt each check runs: what to look at and what counts as done"
    minutes, why = _every(every)
    if minutes is None:
        return None, why
    end, why = _until(until, tz, now)
    if why:
        return None, why
    return (title, prompt, minutes, end), ""


async def task_create(args: dict, ctx: ToolContext) -> ToolResult:
    tz, now = dates.tz(ctx), time.time()
    got, why = _validate(args.get("title"), args.get("prompt"), args.get("every_minutes"), args.get("until"), tz, now)
    if got is None:
        return ToolResult(f"Not created: {why}.", error=True)
    if len(load(ctx.account)) >= MAX_TASKS:
        return ToolResult(
            f"Not created: there are already {MAX_TASKS} background tasks: cancel some first.", error=True
        )
    title, prompt, minutes, end = got
    # It checks later on its own, without the person watching, so they approve it (SPEC §3.4).
    fields = [
        Field("Waiting for", title),
        Field("How to check", prompt, "longtext"),
        Field("Check every (minutes)", str(minutes)),
        Field("Until", _until_text(end, tz)),
    ]
    d, refused = await guard.send(ctx, "task", "Start this background task?", fields)
    if refused:
        return refused
    got, why = _validate(*d.values[:4], tz, time.time())
    if got is None:
        return ToolResult(
            f"The edited details weren't valid ({why}), so nothing was started. Ask the person.", error=True
        )
    title, prompt, minutes, end = got
    entries = load(ctx.account)
    if len(entries) >= MAX_TASKS:
        return ToolResult(f"Not created: there are already {MAX_TASKS} background tasks.", error=True)
    now = time.time()
    entry = {
        "id": secrets.token_hex(3),
        "title": title,
        "prompt": prompt,
        "every": minutes,
        "until": end,
        "next": now + minutes * 60,
        "created": int(now),
        "checks": 0,
    }
    save(ctx.account, [*entries, entry])
    return ToolResult(
        f"Started. {describe(entry, tz)} Checks run when the household is idle, so timing isn't exact. "
        "Tell the person briefly what you'll watch for."
    )


async def task_list(args: dict, ctx: ToolContext) -> ToolResult:
    entries = sorted(load(ctx.account), key=lambda e: e["next"])
    if not entries:
        return ToolResult("No background tasks.")
    tz = dates.tz(ctx)
    return ToolResult("\n".join(describe(e, tz) for e in entries))


async def task_cancel(args: dict, ctx: ToolContext) -> ToolResult:
    task_id = str(args.get("id", "")).strip().strip("`")
    if update(ctx.account, task_id, lambda e: None) is None:
        return ToolResult("There's no background task with that id: use task_list.", error=True)
    return ToolResult("Cancelled.")


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "task_create",
            "Start a background task: when something can't be done yet because it waits on something else (a "
            "reply to an email, a parcel's tracking, a page changing, tickets going on sale), check it on your "
            "own until it happens, then tell the user or do what they asked. Each check runs `prompt` as a new "
            "request, with only your memory: write it so it makes sense on its own later, with what to look at "
            "(addresses, names, tracking codes), how, and what counts as done. Checks run only while nobody is "
            "using Aha!, so timing isn't exact. Not for something at a set time: use schedule_add for that.",
            params(
                ["title", "prompt", "every_minutes", "until"],
                title=string("What you're waiting for, short, e.g. 'Reply from Sam about Saturday'"),
                prompt=string("The instruction each check runs, complete on its own"),
                every_minutes={
                    "type": "integer",
                    "minimum": MIN_EVERY,
                    "description": f"How often to check, in minutes (at least {MIN_EVERY}): as often as it can "
                    "change and matters, e.g. 15 for a reply, 240 for a delivery, 1440 for a price",
                },
                until=string(
                    f"When to stop waiting, local YYYY-MM-DD HH:MM (work it out from the <context> block), or "
                    f"'{NEVER}' for no end"
                ),
            ),
            task_create,
        )
    )
    reg.add(Tool("task_list", "List the user's background tasks, with their ids.", params(), task_list))
    reg.add(
        Tool(
            "task_cancel",
            "Cancel one of the user's background tasks by its id (from task_list).",
            params(["id"], id=string("The id")),
            task_cancel,
        )
    )


# ---- a check's answer


def outcome(answer: str) -> tuple[str, str]:
    """("waiting" | "done" | "failed", the rest). An answer with no marker closes the task as done."""
    found = OUTCOME.match(answer.strip())
    if found is None:
        return "done", answer.strip()
    return found.group(1).lower(), answer.strip()[found.end() :].strip()


def finish(account: Account, task_id: str, answer: str, stopped: bool) -> str | None:
    """After a check: the message for the chat when it closed the task, else None (it waits, quietly)."""
    kind, text = ("waiting", answer) if stopped else outcome(answer)
    if kind == "waiting":
        note = _clean(text, MAX_NOTE)
        update(
            account,
            task_id,
            lambda e: {**e, "last": note, "next": time.time() + e["every"] * 60, "checks": e.get("checks", 0) + 1},
        )
        return None
    entry = update(account, task_id, lambda e: None)
    if entry is None:
        return None  # cancelled while it ran
    account.audit({"background_task_closed": task_id, "outcome": kind})
    head = CLOSED[kind].format(title=entry["title"])
    return f"_{head}_\n\n{text}" if text else f"_{head}_"


def check_prompt(entry: dict, now: float) -> str:
    late = now - entry["next"] > entry["every"] * 60
    return CHECK.format(
        n=entry.get("checks", 0) + 1,
        late=", later than planned: Aha! was busy" if late else "",
        title=entry["title"],
        prompt=entry["prompt"],
        last=LAST_TIME.format(note=entry["last"]) if entry.get("last") else "",
        every=every_words(entry["every"]),
    )


def listing(account: Account) -> list[dict]:
    """For the app's Tasks panel, newest first (SPEC §6.5)."""
    keys = ("id", "title", "prompt", "until", "next", "last", "checks", "created")
    rows = sorted(load(account), key=lambda e: e.get("created", 0), reverse=True)
    return [{**{k: e.get(k) for k in keys}, "every": e["every"] * 60, "last": e.get("last", "")} for e in rows]


# ---- the runner


class Runner:
    """Every CHECK_EVERY seconds, while the household is idle: end expired tasks, run the most overdue check."""

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
                log.exception("background tick failed")
            await asyncio.sleep(CHECK_EVERY)

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        due: list[tuple[float, Account, dict]] = []
        for account in self.accounts.all():
            for entry in load(account):
                if entry.get("until") is not None and entry["until"] <= now:
                    await self._expire(account, entry)
                elif entry["next"] <= now and not self.agent.is_running(account):
                    due.append((entry["next"], account, entry))
        if not due or not self.agent.idle(now) or self.agent.busy():
            return
        _, account, entry = min(due, key=lambda d: d[0])
        self.agent.start_background(account, entry["id"], check_prompt(entry, now))

    async def _expire(self, account: Account, entry: dict) -> None:
        if update(account, entry["id"], lambda e: None) is None:
            return
        account.audit({"background_task_closed": entry["id"], "outcome": "expired"})
        when = dates.for_model(dt.datetime.fromtimestamp(entry["until"], self.tz))
        last = f" The last check found: {entry['last']}" if entry.get("last") else ""
        await self.agent.send(account, EXPIRED.format(title=entry["title"], when=when, last=last))
