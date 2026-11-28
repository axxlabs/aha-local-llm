# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topics that wait (SPEC §4.1): a topic sent to the background wakes up on its own, inside itself.

When the work has to wait (a reply, a delivery, a page changing, a time), the model defers the topic it's working
in (`topic_defer`, a card first): what it waits for, how each wakeup checks, how often (every so many minutes, at
least 5, or at times of day) and what ends it (or never). The schedule is the topic's own (topics.json). Wakeups
run only when the household is idle (nobody typed or answered a card for 5 minutes, no request running), one at a
time, in the topic, with its turns in view, so whatever the person wrote there since is read. A wakeup ends with
check_result: waiting (only its ✓ action lines show), notify (news; it goes on), needs_you (a question; the topic
waits on the person, its wakeups paused until they answer), done or failed (the schedule ends).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from zoneinfo import ZoneInfo

from .. import dates, topics
from ..accounts import Account
from ..approvals import Field
from ..policy import addresses
from . import guard
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

log = logging.getLogger("aha.background")

MAX_SCHEDULED = 20
MIN_EVERY = 5  # minutes
MAX_EVERY = 7 * 24 * 60
MAX_AHEAD = 366 * 86400
MAX_TITLE = 120
MAX_PROMPT = 2000
MAX_LAST = 4000  # the previous wakeup's whole note, given to the next one
IDLE_AFTER = 5 * 60  # seconds without input from anyone before a wakeup runs
CHECK_EVERY = 30.0
CHECKS = "Checks (every N minutes, or times of day like 08:00, 18:00)"
MAX_AT = 6
DONE_WHEN = "Done when (or never)"
MAX_DONE_WHEN = 300
NEVER = "never"
OUTCOMES = ("waiting", "notify", "needs_you", "done", "failed")

WAKE = (
    "[This topic woke up on its own, check {n}{late}; the user isn't looking at the chat]\n"
    "Waiting for: {title}\n\nHow to check: {prompt}\n\nDone when: {done_when}\n\n{last}"
    "The conversation above is this topic so far: what the user wrote in it since it was set up changes what to "
    "do. Check now: open and read what you're checking before you answer, never answer from memory. Finish by "
    "calling check_result with one outcome and its message:\n"
    "waiting: one line on what you found, when it hasn't happened yet. Nothing is shown to the user; you'll check "
    "again in {every}.\n"
    "notify: the message for the user, when there's news and it goes on. Only what's new since last time.\n"
    "needs_you: the question for the user, when you can't go on without their answer (a choice, a detail only "
    "they know). Checks pause until they answer in this topic.\n"
    'done: the message for the user, when what "Done when" says has happened (and what was to be done then is '
    "done). {never}\n"
    "failed: the message for the user, when it can't happen any more (cancelled, delivered elsewhere, sold out "
    "for good). A page that didn't load or that you couldn't find isn't that: report waiting with what went "
    "wrong, and the next check tries again (from the site's home page). Only if the last check couldn't read it "
    "either, report failed."
)
REPORT_RETRY = (
    "[sha check] You haven't called check_result. Call it now with the outcome that fits what you found (waiting "
    "when what you wait for hasn't happened yet) and the message, and never mention this check."
)
LAST_TIME = "The last check ({when}) {how}:\n{note}\n\n"
LAST_HOW = {
    "waiting": "reported waiting",
    "notify": "reported news, and this was sent to the user",
    "needs_you": "asked the user, and they answered in this topic since",
    "ended_early": "ended before it reported (stopped, interrupted, or no check_result); the last note",
}
NEVER_DONE = (
    'This topic has no end ("Done when: never"): it goes on until the user stops it, so never report done; '
    "report notify for news."
)


# ---- the schedule, in its topic


def schedule_of(account: Account, n: int) -> dict | None:
    entry = topics.get(account, n)
    return (entry or {}).get("schedule")


def change(account: Account, n: int, fn) -> dict | None:
    """Change topic n's schedule (fn returns the new one, or None to end it). Returns the topic as it is now."""

    def edit(e: dict) -> dict:
        if not e.get("schedule"):
            return e
        s = fn(dict(e["schedule"]))
        if s is None:
            e = {k: v for k, v in e.items() if k != "schedule"}
            if e.get("state") == "scheduled":
                e["state"] = ""
            return e
        return {**e, "schedule": s}

    return topics.update(account, n, edit)


def run_now(account: Account, n: int) -> bool:
    """The person's Run now (SPEC §4.1): an extra wakeup as soon as the model is free, without waiting for the
    idle time. The planned next one keeps its time. False if the topic has no schedule."""
    if schedule_of(account, n) is None:
        return False
    change(account, n, lambda s: {**s, "now": True, "paused": False})
    return True


def stop(account: Account, n: int) -> dict | None:
    """The schedule ends (the person's Stop, or the model's topic_stop): the topic is a plain one again."""
    if schedule_of(account, n) is None:
        return None
    return change(account, n, lambda s: None)


def resume(account: Account, n: int) -> None:
    """The person answered in a topic whose wakeups waited on them: they go on, from now."""
    s = schedule_of(account, n)
    if s and s.get("paused"):
        change(account, n, lambda s: {**s, "paused": False, "next": next_check(s, time.time())})


def _urls(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in addresses.URL_IN_TEXT.findall(text or "")))


def links_of(account: Account, n: int) -> list[str]:
    """The addresses a topic's wakeups may open as given (SPEC §5.2 "Made-up addresses"): what the request that
    deferred it was shown, or the person wrote in it."""
    return [u for u in (schedule_of(account, n) or {}).get("links", []) if isinstance(u, str)]


def every_words(minutes: int) -> str:
    if minutes % 1440 == 0:
        return "1 day" if minutes == 1440 else f"{minutes // 1440} days"
    if minutes % 60 == 0:
        return "1 hour" if minutes == 60 else f"{minutes // 60} hours"
    return f"{minutes} minutes"


def schedule_words(s: dict) -> str:
    """'every 30 minutes', 'every day at 08:00 and 18:00'."""
    if s.get("at"):
        return "every day at " + " and ".join(s["at"])
    return "every " + every_words(s["every"])


def describe(n: int, s: dict, tz: ZoneInfo) -> str:
    nxt = dates.for_model(dt.datetime.fromtimestamp(s["next"], tz))
    return (
        f"topic {n}, waiting for {s['title']}: {schedule_words(s)}, done when: {s['done_when']}, next check {nxt}, "
        f"{s.get('checks', 0)} checks so far."
    )


# ---- validation


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


def next_check(s: dict, now: float) -> float:
    """When it wakes next after `now`: its next time of day (in its own time zone), or its interval."""
    if s.get("at"):
        tz = dates.tz(s.get("tz") or "UTC")
        day = dt.datetime.fromtimestamp(now, tz).date()
        for k in range(0, 3):
            for t in s["at"]:
                h, m = (int(x) for x in t.split(":"))
                when = dt.datetime.combine(day + dt.timedelta(days=k), dt.time(h, m), tz).timestamp()
                if when > now + 30:
                    return when
    return now + s["every"] * 60


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
    """What ends it, or NEVER; "" when not given."""
    text = _clean(value, MAX_DONE_WHEN)
    return NEVER if text.lower().rstrip(".") == NEVER else text


def _validate(title, prompt, every, at, done_when):
    title, prompt = _clean(title, MAX_TITLE), _prompt(prompt)
    if not title:
        return None, "give a short title: what you're waiting for"
    if not prompt:
        return None, "give `prompt`: how each wakeup checks, what to look at and how"
    ends = _done_when(done_when)
    if not ends:
        return None, (
            f"give `done_when`: what ends it (like 'Sam replied about Saturday'), or '{NEVER}' for one that goes "
            "on until the user stops it"
        )
    schedule, why = _schedule(every, at)
    if schedule is None:
        return None, why
    minutes, times = schedule
    return (title, prompt, minutes, times, ends), ""


# ---- tools


async def topic_defer(args: dict, ctx: ToolContext) -> ToolResult:
    n = ctx.task.topic
    if not n or topics.get(ctx.account, n) is None:
        return ToolResult("There's no topic to defer here.", error=True)
    if ctx.task.wake:
        return ToolResult("This topic already wakes up on its own: report with check_result.", error=True)
    got, why = _validate(
        args.get("waiting_for"), args.get("prompt"), args.get("every_minutes"), args.get("at"), args.get("done_when")
    )
    if got is None:
        return ToolResult(f"Not deferred: {why}.", error=True)
    others = [e for e in topics.all_topics(ctx.account) if e.get("schedule") and e["n"] != n]
    if len(others) >= MAX_SCHEDULED:
        return ToolResult(f"Not deferred: {MAX_SCHEDULED} topics already wake up on their own.", error=True)
    title, prompt, minutes, times, ends = got
    # Its wakeups open only addresses this request was shown (SPEC §5.2 "Made-up addresses"): one written from
    # memory would be trusted by every wakeup (seen: a tracking address, a 404, the task ended).
    model_urls, made_up = _urls(prompt), [u for u in _urls(prompt) if addresses.made_up(u, ctx.task.seen_urls)]
    if made_up:
        return ToolResult(
            f"Not deferred: {', '.join(made_up)} isn't in anything you read in this request or the user sent, so you "
            "put it together yourself, and such addresses are usually wrong. Write the site's name instead (like "
            "fedex.com) and how to find the page there (its tracking box, with the number), then call topic_defer "
            "again.",
            error=True,
        )
    # It goes on later on its own, without the person watching, so they approve it (SPEC §4.1).
    fields = [
        Field("Waiting for", title),
        Field("How to check", prompt, "longtext"),
        Field(DONE_WHEN, ends),
        Field(CHECKS, ", ".join(times) if times else str(minutes)),
    ]
    d, refused = await guard.send(ctx, "task", "Check on this later, on its own?", fields)
    if refused:
        return refused
    v = d.values
    when = v[3].strip()
    got, why = _validate(v[0], v[1], None if ":" in when else when, when if ":" in when else None, v[2])
    if got is None:
        return ToolResult(
            f"The edited details weren't valid ({why}), so nothing was deferred. Ask the person.", error=True
        )
    title, prompt, minutes, times, ends = got
    now = time.time()
    s = {
        "title": title,
        "prompt": prompt,
        "done_when": ends,
        "every": minutes,
        **({"at": times, "tz": ctx.config.timezone} if times else {}),
        "created": int(now),
        "checks": 0,
        **({"card": d.card} if d.card else {}),
        # What its wakeups may open: the prompt's addresses this request was shown, and any the person wrote in it
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
    s["next"] = next_check(s, now)
    topics.update(ctx.account, n, lambda e: {**e, "schedule": s, "state": "scheduled"})
    tz = dates.tz(ctx)
    return ToolResult(
        f"Deferred: {describe(n, s, tz)} Wakeups run when the household is idle, so timing isn't exact. Tell the "
        "person briefly what you'll watch for.",
        did=f"Set topic {n} to check on its own: {schedule_words(s)}, waiting for {title}",
    )


async def topic_stop(args: dict, ctx: ToolContext) -> ToolResult:
    try:
        n = int(str(args.get("number", "")).strip().lstrip("#"))
    except ValueError:
        return ToolResult("Give the topic's number.", error=True)
    entry = topics.get(ctx.account, n)
    if not entry or not entry.get("schedule"):
        return ToolResult(f"Topic {n} doesn't check on its own.", error=True)
    stop(ctx.account, n)
    return ToolResult("Stopped.", did=f"Stopped topic {n} checking on its own ({entry['schedule']['title']})")


async def check_result(args: dict, ctx: ToolContext) -> ToolResult:
    """How a wakeup reports what it found (SPEC §4.1): a tool call, not a word at the start of its answer."""
    if ctx.task.report is None:
        return ToolResult("Only a topic's wakeup reports with check_result: answer the person.", error=True)
    outcome = str(args.get("outcome", "")).strip().lower()
    if outcome not in OUTCOMES:
        return ToolResult(f"outcome must be one of: {', '.join(OUTCOMES)}.", error=True)
    message = str(args.get("message", "")).strip()
    if outcome != "waiting" and not message:
        return ToolResult("Give the message for the user.", error=True)
    ctx.task.report.update(outcome=outcome, message=message)
    return ToolResult("Reported. The check is over.")


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "topic_defer",
            "Send the topic you're working in to the background: the only way to do anything later. It wakes up "
            "on its own, in this topic, until what you wait for happens: a reply (an email, a WhatsApp message), a "
            "parcel's tracking, a page changing, tickets going on sale; or at a time of day, once or repeating "
            "('remind me at 6 to call Sam', 'every morning summarise my email'). How often is all you give: "
            "`every_minutes` (an interval from now) or `at` (times of day, every day). Say in `done_when` what ends "
            "it, always: the event that closes it ('Sam replied about Saturday', 'the parcel is delivered', "
            "'reminded once'), or 'never' for watching that goes on until the user stops it. Each wakeup sees this "
            "topic's conversation and `prompt`: write in it what to look at (the site by its name, like fedex.com, "
            "and how to find the page there; names, tracking codes) and how. Put an address in it only if you "
            "opened it or the user sent it in this request: never one you put together. Wakeups run only while "
            "nobody is using sha, so timing isn't exact.",
            params(
                ["waiting_for", "prompt", "done_when"],
                waiting_for=string("What you're waiting for, short, e.g. 'Reply from Sam about Saturday'"),
                prompt=string("How each wakeup checks, complete on its own"),
                done_when=string(
                    "What ends it: the event that closes it, e.g. 'Sam replied about Saturday', 'the parcel is "
                    f"delivered'; or '{NEVER}' for watching or a repeat that goes on until the user stops it"
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
                    "e.g. ['08:00'] for every morning, ['18:00'] for every evening",
                },
            ),
            topic_defer,
        )
    )
    reg.add(
        Tool(
            "topic_stop",
            "Stop a topic from checking on its own (the active ones are listed in the context), when the user asks.",
            params(["number"], number=string("The topic's number")),
            topic_stop,
        )
    )
    reg.add(
        Tool(
            "check_result",
            "Only when a topic woke up on its own: report what you found, to end the check.",
            params(
                ["outcome", "message"],
                outcome={
                    "type": "string",
                    "enum": list(OUTCOMES),
                    "description": "waiting, notify, needs_you, done or failed",
                },
                message=string("For waiting: one line on what you found. Otherwise: the message for the user"),
            ),
            check_result,
        )
    )


# ---- after a wakeup


def _last(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())[:MAX_LAST]


def finish(account: Account, n: int, outcome: str, message: str, stopped: bool) -> str:
    """After a wakeup: what to do with its message. "" when there's nothing to show (waiting, or it ended early);
    else the outcome (notify, needs_you, done, failed), and the topic's state is set for it. Its note goes whole
    to the next wakeup (SPEC §4.1)."""
    text = message.strip()
    kind = outcome if outcome in OUTCOMES and not stopped else "ended_early"
    s = schedule_of(account, n)
    if s is None:
        return ""  # stopped while it ran
    if kind == "done" and s.get("done_when") == NEVER:
        kind = "notify"  # it has no end: news, and it goes on (only the person stops it)
    if kind in ("done", "failed"):
        account.audit({"topic_schedule_closed": n, "outcome": kind})
        topics.update(account, n, lambda e: {**{k: v for k, v in e.items() if k != "schedule"}, "state": kind})
        return kind
    now = time.time()

    def checked(s: dict) -> dict:
        # A Run now wakeup is an extra one: the planned next keeps its time.
        later = s["next"] if s.get("now") else next_check(s, now)
        return {
            **{k: v for k, v in s.items() if k != "now"},
            "last": _last(text) if text else s.get("last", ""),
            "last_how": kind,
            "last_at": now,
            "next": later,
            "checks": s.get("checks", 0) + 1,
            "updates": s.get("updates", 0) + int(kind in ("notify", "needs_you")),
            **({"paused": True} if kind == "needs_you" else {}),
        }

    change(account, n, checked)
    if kind == "needs_you":
        topics.wait_on_person(account, n, text)
    return kind if kind in ("notify", "needs_you") else ""


def interrupted(account: Account, n: int) -> None:
    """A wakeup stopped by someone's input or Stop: it ended early, and the next planned one tries again."""
    change(
        account,
        n,
        lambda s: {**{k: v for k, v in s.items() if k != "now"}, "last_how": "ended_early", "last_at": time.time()},
    )


EDITABLE = ("title", "prompt", "done_when", "every", "at", "next")  # what the topic's strip sends


def edit(account: Account, n: int, changes: dict) -> str:
    """The person's own edit of a topic's schedule (its strip's Edit): "" when done, else why not.
    `every` in seconds; `next` epoch seconds; a `next` in the past is due now."""
    s = schedule_of(account, n)
    if s is None:
        return "it doesn't check on its own"
    new, now = dict(s), time.time()
    if "title" in changes:
        new["title"] = _clean(changes["title"], MAX_TITLE)
        if not new["title"]:
            return "what it waits for can't be empty"
    if "prompt" in changes:
        new["prompt"] = _prompt(changes["prompt"])
        if not new["prompt"]:
            return "how it checks can't be empty"
        new["links"] = _urls(new["prompt"])  # the person's own words: its addresses are theirs
    if "done_when" in changes:
        new["done_when"] = _done_when(changes["done_when"])
        if not new["done_when"]:
            return f"say what ends it, or {NEVER}"
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
        new.setdefault("tz", s.get("tz") or "UTC")
    if "next" in changes:
        if not isinstance(changes["next"], int | float) or changes["next"] > now + MAX_AHEAD:
            return "the next check must be a time within a year"
        new["next"] = max(float(changes["next"]), now)
    change(account, n, lambda _: new)
    return ""


def _last_line(s: dict, tz: ZoneInfo) -> str:
    how = s.get("last_how", "")
    if not how:
        return ""
    when = dates.for_model(dt.datetime.fromtimestamp(s.get("last_at") or s["next"], tz))
    note = s.get("last") or "(nothing)"
    return LAST_TIME.format(when=when, how=LAST_HOW.get(how, LAST_HOW["waiting"]), note=note)


def wake_prompt(s: dict, now: float, tz: ZoneInfo) -> str:
    late = now - s["next"] > s["every"] * 60
    done_when = s["done_when"]
    return WAKE.format(
        done_when=done_when,
        never=NEVER_DONE if done_when == NEVER else "",
        n=s.get("checks", 0) + 1,
        late=", later than planned: sha was busy" if late else "",
        title=s["title"],
        prompt=s["prompt"],
        last=_last_line(s, tz),
        every=schedule_words(s).removeprefix("every "),
    )


# ---- the runner


class Runner:
    """Every CHECK_EVERY seconds, while the household is idle: wake the most overdue topic."""

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
        due: list[tuple[float, Account, int, dict]] = []
        for account in self.accounts.all():
            for entry in topics.all_topics(account):
                s = entry.get("schedule")
                if not s or (s.get("paused") and not s.get("now")):
                    continue
                if (s["next"] <= now or s.get("now")) and not self.agent.is_running(account):
                    due.append((s["next"], account, entry["n"], s))
        asked = [d for d in due if d[3].get("now")]  # Run now: no idle wait, only a free model
        if self.agent.busy() or not (asked or (due and self.agent.idle(now))):
            return
        _, account, n, s = min(asked or due, key=lambda d: d[0])
        await self.agent.start_wake(account, n, wake_prompt(s, now, self.tz))
