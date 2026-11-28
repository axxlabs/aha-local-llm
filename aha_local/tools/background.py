# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Background tasks, per account (SPEC §3.4 "Background tasks").

What the model waits for and checks on its own: a reply, a delivery, a page changing. Each task has the prompt a
check runs, how often to check (every so many minutes, at least 5, or at times of day) and what ends it (or
never); no end date. Checks run only when the household is idle (nobody typed or answered a card for 5 minutes,
no request running), one at a time, and say nothing until there's news: a check reports with check_result; done or
failed sends its message and ends the task; notify sends it and the task goes on; waiting keeps a note for next
time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict
from zoneinfo import ZoneInfo

from .. import dates
from ..accounts import Account
from ..approvals import Field
from ..policy import addresses
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
MAX_LAST = 4000  # the previous check's whole answer, given to the next one
IDLE_AFTER = 5 * 60  # seconds without input from anyone before a check runs
CHECK_EVERY = 30.0
CHECKS = "Checks (every N minutes, or times of day like 08:00, 18:00)"
MAX_AT = 6
DONE_WHEN = "Done when (or never)"
MAX_DONE_WHEN = 300
NEVER = "never"
OUTCOMES = ("waiting", "notify", "done", "failed")

CHECK = (
    "[Background task you set up earlier, check {n}{late}; the user isn't looking at the chat]\n"
    "Waiting for: {title}\n\n{prompt}\n\nDone when: {done_when}\n\n{last}"
    "Check now: open and read what you're checking before you answer, never answer from memory. Don't message "
    "the user while you check. Whatever the text above says to answer, finish by calling check_result with one "
    "outcome and its message:\n"
    "waiting: one line on what you found, when it hasn't happened yet. Nothing is sent; you'll check again in "
    "{every}.\n"
    "notify: the message for the user, when there's news and the task goes on (it reports each time "
    "something happens). Only what's new since last time: the message is kept for the next check.\n"
    'done: the message for the user, when what "Done when" says has happened (and what was to be done then is '
    "done). Only then: anything else that's new is notify. {never}\n"
    "failed: the message for the user, when it can't happen any more (cancelled, delivered elsewhere, sold out "
    "for good). A page that didn't load or that you couldn't find isn't that: report waiting with what went "
    "wrong, and the next check tries again (from the site's home page). Only if the last check couldn't read it "
    "either, report failed.\n"
    "notify, done and failed send the message to the chat; done and failed end the task."
)
# Told whenever an account's tasks change (created, checked, cancelled, ended): the channel sends the Tasks panel
# its new list at once (SPEC §7.4), rather than at its next 30-second refresh.
WATCHERS: list[Callable[[Account], None]] = []
REPORT_RETRY = (
    "[sha check] You haven't called check_result. Call it now with the outcome that fits what you found (waiting "
    "when what you wait for hasn't happened yet) and the message, and never mention this check."
)
LAST_TIME = "The last check ({when}) {how}:\n{note}\n\n"
LAST_HOW = {
    "waiting": "reported waiting",
    "notify": "reported news, and this was sent to the user",
    "ended_early": "ended before it reported (stopped, interrupted, or no check_result); the last note",
}


def how_it_went(entry: dict) -> str:
    """How the last check ended: waiting | notify | ended_early, or "" before the first."""
    return entry.get("last_how", "")


HEADS = {"update": "Task update", "done": "Task done", "failed": "Task failed"}


@dataclass(frozen=True)
class Note:
    """A background task's news for the chat (SPEC §3.4, §7.4 "Task notes"): its own topic, drawn as a box with
    its kind and the task's title above the message."""

    kind: str  # update | done | failed
    title: str
    text: str
    prompt: str = ""  # the task's full instruction: the box's (i)
    every: int = 0  # minutes between checks
    id: str = ""  # the task's: the Tasks panel links its last check to this message (SPEC §7.4)

    def for_app(self) -> dict:
        return {
            "kind": self.kind,
            "title": self.title,
            "prompt": self.prompt,
            "every": self.every * 60,
            **({"id": self.id} if self.id else {}),
        }

    @property
    def head(self) -> str:
        return f"{HEADS[self.kind]}: {self.title}"

    def as_text(self) -> str:
        """The note as words (the recent turns, a push's preview, an older app)."""
        return f"{self.head}\n\n{self.text}" if self.text else self.head


# ---- storage


class Entry(TypedDict, total=False):
    """One background task as stored in tasks.json: every key a task can have, in one place."""

    id: str  # six hex characters
    title: str  # what it waits for
    prompt: str  # what each check runs
    done_when: str  # what ends it, or "never": every check is told, and a never-ending one can't be closed
    every: int  # minutes between checks (with `at`, a day divided by its times)
    at: list[str]  # times of day it checks, "08:00", in `tz`; none: every `every` minutes
    tz: str  # the household's time zone when `at` was set
    next: float  # epoch seconds of the next planned check
    created: int
    checks: int  # checks run so far
    updates: int  # of them, the ones that told the person
    last: str  # the last check's message, given whole to the next
    last_how: str  # how the last check ended: waiting | notify | ended_early (how_it_went)
    last_at: float
    card: str  # the approval card that started it (the app's fold-line icons)
    links: list[str]  # the addresses its checks may open (SPEC §5.2 "Made-up addresses")
    allowed: list[str]  # what the person ticked "Always allow for this task" on in its checks (guard.allow_key)
    now: bool  # Run now: an extra check as soon as the model is free; the planned next check keeps its time


def load(account: Account) -> list[Entry]:
    return account.vault.read_json(account.dir / FILE, []) or []


def save(account: Account, entries: list[Entry]) -> None:
    path = account.dir / FILE
    if entries:
        account.vault.write_json(path, entries)
    else:
        path.unlink(missing_ok=True)
    changed(account)


def changed(account: Account) -> None:
    """Tell the watchers (the open Tasks panels) the account's tasks changed, or a check started or ended."""
    for watcher in WATCHERS:
        watcher(account)


def run_now(account: Account, task_id: str) -> bool:
    """The person's Run now (SPEC §3.4): an extra check, as soon as the model is free, without waiting for the
    idle time. The planned next check keeps its time. False if there's no such task."""
    return update(account, task_id, lambda e: {**e, "now": True}) is not None


def _urls(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in addresses.URL_IN_TEXT.findall(text or "")))


def links_of(account: Account, task_id: str) -> list[str]:
    """The addresses a task's checks may open as given (SPEC §5.2 "Made-up addresses"): what the request that
    started it was shown, or the person wrote in it. A task kept before tasks kept them has none."""
    entry = next((e for e in load(account) if e.get("id") == task_id), None)
    return [u for u in (entry or {}).get("links", []) if isinstance(u, str)]


def allowed_of(account: Account, task_id: str) -> set[str]:
    """What the person ticked "always" on in this task's checks (SPEC §5.3): every check runs with it."""
    entry = next((e for e in load(account) if e.get("id") == task_id), None)
    return {k for k in (entry or {}).get("allowed", []) if isinstance(k, str)}


def allow(account: Account, task_id: str, key: str) -> None:
    """A card in one of its checks was ticked "always": its later checks don't ask for it (SPEC §5.3)."""
    update(account, task_id, lambda e: {**e, "allowed": sorted({*e.get("allowed", []), key})})


def update(account: Account, task_id: str, fn: Callable[[Entry], Entry | None]) -> Entry | None:
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


def describe(entry: Entry, tz: ZoneInfo) -> str:
    end = f"done when: {entry['done_when']}"
    nxt = dates.for_model(dt.datetime.fromtimestamp(entry["next"], tz))
    last = f" Last found: {entry['last']}" if entry.get("last") else ""
    return (
        f"`{entry['id']}` {entry['title']}: {schedule_words(entry)}, {end}, next check {nxt}, "
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


AT = re.compile(r"^([01]?\d|2[0-3])[:h.]([0-5]\d)$")


def _at(value) -> tuple[list[str] | None, str]:
    """Times of day ("08:00"), sorted, from a list or "08:00, 18:00"; None with why when one isn't a time."""
    items = value if isinstance(value, list) else re.split(r"[,;\s]+", str(value or ""))
    times = []
    for item in items:
        text = str(item).strip().lower().removeprefix("at ")
        if not text:
            continue
        m = AT.match(text)
        if not m:
            return None, f"{item!r} isn't a time of day: give times like 08:00 or 18:30"
        times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    if len(times) > MAX_AT:
        return None, f"at most {MAX_AT} times a day"
    return sorted(set(times)), ""


def next_check(entry: dict, now: float) -> float:
    """When the task checks next after `now`: its next time of day (in its own time zone), or its interval."""
    if entry.get("at"):
        tz = dates.tz(entry.get("tz") or "UTC")
        day = dt.datetime.fromtimestamp(now, tz).date()
        for k in range(0, 3):
            for t in entry["at"]:
                h, m = (int(x) for x in t.split(":"))
                when = dt.datetime.combine(day + dt.timedelta(days=k), dt.time(h, m), tz).timestamp()
                if when > now + 30:
                    return when
    return now + entry["every"] * 60


def schedule_words(entry: dict) -> str:
    """'every 30 minutes', 'every day at 08:00 and 18:00'."""
    if entry.get("at"):
        return "every day at " + " and ".join(entry["at"])
    return "every " + every_words(entry["every"])


def _schedule(every, at) -> tuple[tuple[int, list[str]] | None, str]:
    """(minutes, times of day) from the tool or the card: one of the two, never both."""
    times, why = _at(at) if at not in (None, "", []) else ([], "")
    if times is None:
        return None, why
    if times:
        if every not in (None, ""):
            return None, "give `every_minutes` or `at`, not both"
        return (1440 // len(times), times), ""
    if every in (None, ""):
        return None, "give `every_minutes` (how often) or `at` (times of day, like 08:00 for every morning)"
    minutes, why = _every(every)
    return ((minutes, []), "") if minutes is not None else (None, why)


def _done_when(value) -> str:
    """What ends the task, or NEVER; "" when not given."""
    text = _clean(value, MAX_DONE_WHEN)
    return NEVER if text.lower().rstrip(".") == NEVER else text


def _validate(title, prompt, every, at, done_when):
    title, prompt = _clean(title, MAX_TITLE), _prompt(prompt)
    if not title:
        return None, "give a short title: what you're waiting for"
    if not prompt:
        return None, "give the prompt each check runs: what to look at and how"
    ends = _done_when(done_when)
    if not ends:
        return None, (
            f"give `done_when`: what ends this task (like 'Sam replied about Saturday'), or '{NEVER}' for one that "
            "goes on until the user cancels it"
        )
    schedule, why = _schedule(every, at)
    if schedule is None:
        return None, why
    minutes, times = schedule
    return (title, prompt, minutes, times, ends), ""


async def task_create(args: dict, ctx: ToolContext) -> ToolResult:
    tz = dates.tz(ctx)
    got, why = _validate(
        args.get("title"), args.get("prompt"), args.get("every_minutes"), args.get("at"), args.get("done_when")
    )
    if got is None:
        return ToolResult(f"Not created: {why}.", error=True)
    if len(load(ctx.account)) >= MAX_TASKS:
        return ToolResult(
            f"Not created: there are already {MAX_TASKS} background tasks: cancel some first.", error=True
        )
    title, prompt, minutes, times, ends = got
    # Its checks open only addresses this request was shown (SPEC §5.2 "Made-up addresses"): one written from
    # memory into the task would be trusted by every check (seen: a tracking address, a 404, the task ended).
    model_urls, made_up = _urls(prompt), [u for u in _urls(prompt) if addresses.made_up(u, ctx.task.seen_urls)]
    if made_up:
        return ToolResult(
            f"Not created: {', '.join(made_up)} isn't in anything you read in this request or the user sent, so you "
            "put it together yourself, and such addresses are usually wrong. Write the site's name instead (like "
            "fedex.com) and how to find the page there (its tracking box, with the number), then call task_create "
            "again.",
            error=True,
        )
    # It checks later on its own, without the person watching, so they approve it (SPEC §3.4).
    fields = [
        Field("Waiting for", title),
        Field("How to check", prompt, "longtext"),
        Field(DONE_WHEN, ends),
        # How often, and nothing else: no start or end date (2026-11-26); what ends it is `done_when`.
        Field(CHECKS, ", ".join(times) if times else str(minutes)),
    ]
    d, refused = await guard.send(ctx, "task", "Start this background task?", fields)
    if refused:
        return refused
    v = d.values
    when = v[3].strip()
    got, why = _validate(v[0], v[1], None if ":" in when else when, when if ":" in when else None, v[2])
    if got is None:
        return ToolResult(
            f"The edited details weren't valid ({why}), so nothing was started. Ask the person.", error=True
        )
    title, prompt, minutes, times, ends = got
    entries = load(ctx.account)
    if len(entries) >= MAX_TASKS:
        return ToolResult(f"Not created: there are already {MAX_TASKS} background tasks.", error=True)
    now = time.time()
    entry = {
        "id": secrets.token_hex(3),
        "title": title,
        "prompt": prompt,
        "done_when": ends,
        # What the person ticked "always" on in the request that starts it carries over to its checks (SPEC §5.2)
        **({"allowed": sorted(ctx.task.allowed)} if ctx.task.allowed else {}),
        "every": minutes,
        **({"at": times, "tz": ctx.config.timezone} if times else {}),
        "created": int(now),
        "checks": 0,
        **({"card": d.card} if d.card else {}),
        # What its checks may open: the prompt's addresses this request was shown, and any the person wrote in it
        # on the card (their own words).
        **(
            {"links": links}
            if (
                links := [
                    u for u in _urls(prompt) if u not in model_urls or not addresses.made_up(u, ctx.task.seen_urls)
                ]
            )
            else {}
        ),
    }
    entry["next"] = next_check(entry, now)
    save(ctx.account, [*entries, entry])
    return ToolResult(
        f"Started. {describe(entry, tz)} Checks run when the household is idle, so timing isn't exact. "
        "Tell the person briefly what you'll watch for.",
        did=f"Started the background task {describe(entry, tz)}",
    )


async def task_list(args: dict, ctx: ToolContext) -> ToolResult:
    entries = sorted(load(ctx.account), key=lambda e: e["next"])
    if not entries:
        return ToolResult("No background tasks.")
    tz = dates.tz(ctx)
    return ToolResult("\n".join(describe(e, tz) for e in entries))


async def task_cancel(args: dict, ctx: ToolContext) -> ToolResult:
    task_id = str(args.get("id", "")).strip().strip("`")
    if (gone := update(ctx.account, task_id, lambda e: None)) is None:
        return ToolResult("There's no background task with that id: use task_list.", error=True)
    return ToolResult("Cancelled.", did=f"Cancelled the background task “{gone['title']}” [{task_id}].")


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "task_create",
            "Start a background task: the only way to do anything later. For something that waits on something "
            "else (a reply to an email, a parcel's tracking, a page changing, tickets going on sale): check it until "
            "it happens, then tell the user or do what they asked. For watching that goes on ('tell me whenever "
            "Sam emails'): each check reports only what's new. For something at a time of day, once or repeating "
            "('remind me at 6 to call Sam', 'every morning summarise my email'): `at` that time, and the check does "
            "it. How often is all you give: `every_minutes` (an interval from now) or `at` (times of day, every "
            "day: every morning 08:00, every evening 18:00); there's no start or end date. Say in `done_when` what "
            "ends it, always: the event that closes it ('Sam replied about Saturday', 'the parcel is delivered', "
            "'Meta AI sent the drawing', 'reminded once'), or 'never' for watching or a repeat that goes on until "
            "the user cancels it. Each check runs `prompt` as a new request, with only your memory: write it so it "
            "makes sense on its own later, with what to look at (the site by its name, like fedex.com, and how to "
            "find the page there; names, tracking codes) and how. Put an address in it only if you opened it or "
            "the user sent it in this request: never one you put together. Don't say in it what to answer: each "
            "check is told how to answer. Checks run only while nobody is using sha, so timing isn't exact.",
            params(
                ["title", "prompt", "done_when"],
                title=string("What you're waiting for, short, e.g. 'Reply from Sam about Saturday'"),
                prompt=string("The instruction each check runs, complete on its own"),
                done_when=string(
                    "What ends the task: the event that closes it, e.g. 'Sam replied about Saturday', 'the parcel "
                    f"is delivered'; or '{NEVER}' for watching or a repeat that goes on until the user cancels it"
                ),
                every_minutes={
                    "type": "integer",
                    "minimum": MIN_EVERY,
                    "description": f"How often to check, in minutes (at least {MIN_EVERY}), from now: as often as "
                    "it can change and matters, e.g. 15 for a reply, 240 for a delivery, 1440 for a price. Or `at`",
                },
                at={
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Instead of every_minutes: local times of day it checks, every day, HH:MM, "
                    "e.g. ['08:00'] for every morning, ['18:00'] for every evening, ['08:00', '18:00'] for both",
                },
            ),
            task_create,
        )
    )
    reg.add(Tool("task_list", "List the user's background tasks, with their ids.", params(), task_list))
    reg.add(
        Tool(
            "check_result",
            "Only in a background task's check: report what you found, to end the check.",
            params(
                ["outcome", "message"],
                outcome={"type": "string", "enum": list(OUTCOMES), "description": "waiting, notify, done or failed"},
                message=string("For waiting: one line on what you found. Otherwise: the message for the user"),
            ),
            check_result,
        )
    )
    reg.add(
        Tool(
            "task_cancel",
            "Cancel one of the user's background tasks by its id (from task_list).",
            params(["id"], id=string("The id")),
            task_cancel,
        )
    )


# ---- a check's answer


async def check_result(args: dict, ctx: ToolContext) -> ToolResult:
    """How a check reports what it found (SPEC §3.4): a tool call, not a word at the start of its answer to parse."""
    if ctx.task.report is None:
        return ToolResult("Only a background task's check reports with check_result: answer the person.", error=True)
    outcome = str(args.get("outcome", "")).strip().lower()
    if outcome not in OUTCOMES:
        return ToolResult(f"outcome must be one of: {', '.join(OUTCOMES)}.", error=True)
    message = str(args.get("message", "")).strip()
    if outcome != "waiting" and not message:
        return ToolResult("Give the message for the user.", error=True)
    ctx.task.report.update(outcome=outcome, message=message)
    return ToolResult("Reported. The check is over.")


def _last(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())[:MAX_LAST]


def finish(account: Account, task_id: str, outcome: str, message: str, stopped: bool) -> Note | None:
    """After a check: its note for the chat when there's news, else None. done and failed end the task; waiting
    and notify keep it; a check that stopped or never reported (even when asked again) ended early: nothing is
    sent, and the next planned check tries again. Whatever it was, its message goes, whole, to the next check
    (SPEC §3.4)."""
    text = message.strip()
    kind = outcome if outcome in OUTCOMES and not stopped else "ended_early"
    entry = next((e for e in load(account) if e.get("id") == task_id), None)
    if kind == "done" and entry is not None and entry.get("done_when") == NEVER:
        kind = "notify"  # it has no end: news, and it goes on (only the person cancels it)
    if kind in ("done", "failed"):
        entry = update(account, task_id, lambda e: None)
        if entry is None:
            return None  # cancelled while it ran
        account.audit({"background_task_closed": task_id, "outcome": kind})
        return Note(kind, entry["title"], text, entry["prompt"], entry["every"], task_id)

    def checked(e: dict) -> dict:
        now = time.time()
        # A Run now check is an extra one: the planned next check keeps its time.
        later = e["next"] if e.get("now") else next_check(e, now)
        return {
            **{k: v for k, v in e.items() if k != "now"},
            "last": _last(text) if text else e.get("last", ""),
            "last_how": kind,
            "last_at": now,
            "next": later,
            "checks": e.get("checks", 0) + 1,
            "updates": e.get("updates", 0) + int(kind == "notify"),
        }

    entry = update(account, task_id, checked)
    if kind != "notify" or entry is None:
        return None  # nothing to tell, or cancelled while it ran
    return Note("update", entry["title"], text, entry["prompt"], entry["every"], task_id)


def interrupted(account: Account, task_id: str) -> None:
    """A check stopped by someone's input or Stop: it ended early, and the next planned check tries again."""
    update(
        account,
        task_id,
        lambda e: {
            **{k: v for k, v in e.items() if k != "now"},
            "last_how": "ended_early",
            "last_at": time.time(),
        },
    )


EDITABLE = ("title", "prompt", "done_when", "every", "at", "next", "allowed")  # what the panel sends


def edit(account: Account, task_id: str, changes: dict) -> str:
    """The person's own edit of a task, any of its fields (the Tasks panel): "" when done, else why not.
    `every` in seconds; `next` epoch seconds; a `next` in the past is due now."""
    entry = next((e for e in load(account) if e["id"] == task_id), None)
    if entry is None:
        return "no such task"
    new, now = dict(entry), time.time()
    if "title" in changes:
        new["title"] = _clean(changes["title"], MAX_TITLE)
        if not new["title"]:
            return "the title can't be empty"
    if "prompt" in changes:
        new["prompt"] = _prompt(changes["prompt"])
        if not new["prompt"]:
            return "what it checks can't be empty"
        new["links"] = _urls(new["prompt"])  # the person's own words: its addresses are theirs
    if "done_when" in changes:
        new["done_when"] = _done_when(changes["done_when"])
        if not new["done_when"]:
            return f"say what ends it, or {NEVER}"
    if "allowed" in changes:  # the person takes an "always" back (or several)
        kept = changes["allowed"] if isinstance(changes["allowed"], list) else []
        new["allowed"] = sorted(k for k in entry.get("allowed", []) if k in kept)
    if "every" in changes:
        if not isinstance(changes["every"], int | float):
            return "how often must be a number"
        minutes, why = _every(changes["every"] / 60)
        if minutes is None:
            return why
        new["every"] = minutes
        new.pop("at", None)  # an interval replaces times of day
    if changes.get("at"):
        times, why = _at(changes["at"])
        if not times:
            return why or "give times of day like 08:00"
        new["at"], new["every"] = times, 1440 // len(times)
        new.setdefault("tz", entry.get("tz") or "UTC")
    if "next" in changes:
        if not isinstance(changes["next"], int | float) or changes["next"] > now + MAX_AHEAD:
            return "the next check must be a time within a year"
        new["next"] = max(float(changes["next"]), now)
    kept = (*EDITABLE, "links")
    update(account, task_id, lambda e: {**e, **{k: new[k] for k in kept if k in new}})
    return ""


def _last_line(entry: Entry, tz: ZoneInfo) -> str:
    how = how_it_went(entry)
    if not how:
        return ""
    when = dates.for_model(dt.datetime.fromtimestamp(entry.get("last_at") or entry["next"], tz))
    note = entry.get("last") or "(nothing)"
    return LAST_TIME.format(when=when, how=LAST_HOW.get(how, LAST_HOW["waiting"]), note=note)


NEVER_DONE = (
    'This task has no end ("Done when: never"): it runs until the user cancels it, so never report done; '
    "report notify for news."
)


def check_prompt(entry: Entry, now: float, tz: ZoneInfo) -> str:
    late = now - entry["next"] > entry["every"] * 60
    done_when = entry["done_when"]
    return CHECK.format(
        done_when=done_when,
        never=NEVER_DONE if done_when == NEVER else "",
        n=entry.get("checks", 0) + 1,
        late=", later than planned: sha was busy" if late else "",
        title=entry["title"],
        prompt=entry["prompt"],
        last=_last_line(entry, tz),
        every=schedule_words(entry).removeprefix("every "),
    )


def listing(account: Account) -> list[dict]:
    """For the app's Tasks panel, newest first (SPEC §6.5)."""
    keys = ("id", "title", "prompt", "done_when", "at", "next", "created", "card")
    # "allowed": what its cards were allowed "always", in the person's words, with each key to take one back
    rows = sorted(load(account), key=lambda e: e.get("created", 0), reverse=True)
    return [
        {
            **{k: e.get(k) for k in keys},
            "every": e["every"] * 60,
            "last": e.get("last", ""),
            "checks": e.get("checks", 0),
            "updates": e.get("updates", 0),
            "now": bool(e.get("now")),
            "last_how": how_it_went(e),
            "allowed": [{"key": k, "label": guard.allow_label(k)} for k in e.get("allowed", [])],
        }
        for e in rows
    ]


# ---- the runner


class Runner:
    """Every CHECK_EVERY seconds, while the household is idle: run the most overdue check."""

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
                if (entry["next"] <= now or entry.get("now")) and not self.agent.is_running(account):
                    due.append((entry["next"], account, entry))
        asked = [d for d in due if d[2].get("now")]  # Run now: no idle wait, only a free model
        if self.agent.busy() or not (asked or (due and self.agent.idle(now))):
            return
        _, account, entry = min(asked or due, key=lambda d: d[0])
        await self.agent.start_background(account, entry["id"], check_prompt(entry, now, self.tz))
