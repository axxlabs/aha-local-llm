# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Background tasks, per account (SPEC §3.4 "Background tasks").

What the model waits for and checks on its own: a reply, a delivery, a page changing. Each task has the prompt a
check runs, how often to check (at least every 5 minutes) and until when (or never). Checks run only when the
household is idle (nobody typed or answered a card for 5 minutes, no request running), one at a time, and say
nothing until there's news: DONE or FAILED sends its message and ends the task; NOTIFY sends it and the task goes
on; WAITING keeps a note for next time.
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
from zoneinfo import ZoneInfo

from .. import dates
from ..accounts import Account
from ..approvals import Field
from ..policy import addresses, grants
from ..policy.grants import Grant, emails_in
from . import guard, trusted
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
SITES = {"type": "array", "items": {"type": "string"}}
MAX_ALLOWED = 20  # sites, and people, each, a new task may ask for
NEVER = "never"
OUTCOME = re.compile(r"^[\W_]*(WAITING|NOTIFY|DONE|FAILED)\b[ \t*_`]*[:\-–—]?[ \t*_`]*", re.I)

CHECK = (
    "[Background task you set up earlier, check {n}{late}; the user isn't looking at the chat]\n"
    "Waiting for: {title}\n\n{prompt}\n\n{last}"
    "Check now: open and read what you're checking before you answer, never answer from memory. Don't message "
    "the user while you check. Whatever the text above says to answer, start your answer with one of:\n"
    "WAITING: one line on what you found, when it hasn't happened yet. Nothing is sent; you'll check again in "
    "{every}.\n"
    "NOTIFY: the message for the user, when there's news and the task goes on (it reports each time "
    "something happens). Only what's new since last time: the message is kept for the next check.\n"
    "DONE: the message for the user, when it has happened (and what was to be done then is done).\n"
    "FAILED: the message for the user, when it can't happen any more (cancelled, delivered elsewhere, sold out "
    "for good). A page that didn't load or that you couldn't find isn't that: answer WAITING with what went "
    "wrong, and the next check tries again (from the site's home page). Only if the last check couldn't read it "
    "either, answer FAILED.\n"
    "NOTIFY, DONE and FAILED send the message to the chat; DONE and FAILED end the task."
)
# Told whenever an account's tasks change (created, checked, cancelled, ended): the channel sends the Tasks panel
# its new list at once (SPEC §7.4), rather than at its next 30-second refresh.
WATCHERS: list[Callable[[Account], None]] = []
OUTCOME_RETRY = (
    "[aha check] Your answer doesn't start with WAITING:, NOTIFY:, DONE: or FAILED:. Answer again, starting with "
    "the one that fits what you found (WAITING: when what you wait for hasn't happened yet), and never mention "
    "this check."
)
LAST_TIME = "The last check ({when}) {how}:\n{note}\n\n"
LAST_HOW = {
    "waiting": "answered WAITING",
    "notify": "answered NOTIFY, and this was sent to the user",
    "unfinished": "didn't finish; what it had",
    "interrupted": "was stopped before it finished (someone started using Aha!)",
}
EXPIRED = "I stopped waiting on {when}.{last}"
HEADS = {"update": "Task update", "done": "Task done", "failed": "Task failed", "expired": "Task expired"}


@dataclass(frozen=True)
class Note:
    """A background task's news for the chat (SPEC §3.4, §7.4 "Task notes"): its own topic, drawn as a box with
    its kind and the task's title above the message."""

    kind: str  # update | done | failed | expired
    title: str
    text: str
    prompt: str = ""  # the task's full instruction: the box's (i)
    every: int = 0  # minutes between checks

    def for_app(self) -> dict:
        return {"kind": self.kind, "title": self.title, "prompt": self.prompt, "every": self.every * 60}

    @property
    def head(self) -> str:
        return f"{HEADS[self.kind]}: {self.title}"

    def as_text(self) -> str:
        """The note as words (the recent turns, a push's preview, an older app)."""
        return f"{self.head}\n\n{self.text}" if self.text else self.head


# ---- storage


def load(account: Account) -> list[dict]:
    return account.vault.read_json(account.dir / FILE, []) or []


def save(account: Account, entries: list[dict]) -> None:
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


def _sites(values: object) -> tuple[list[str], list[str]]:
    """(sites, what wasn't a site)."""
    good, bad = [], []
    for v in values if isinstance(values, list) else []:
        site = trusted.valid(str(v))
        (good if site else bad).append(site or str(v))
    return list(dict.fromkeys(good)), bad


def _people(values: object) -> tuple[list[str], list[str]]:
    good, bad = [], []
    for v in values if isinstance(values, list) else []:
        found = emails_in(str(v))
        (good if len(found) == 1 else bad).append(found[0] if len(found) == 1 else str(v))
    return list(dict.fromkeys(good)), bad


def asked_items(args: dict, trusted_sites: set[str]) -> tuple[list[tuple[str, Grant]], list[str]]:
    """What a new task's checks may do without asking, as the model asked it: one tick each on the card, in the
    person's words, and what wasn't a site or one address. Trusted sites need no tick; typing on a site includes
    opening it, so a site asked for both is one tick."""
    opens, bad_opens = _sites(args.get("open"))
    types, bad_types = _sites(args.get("type"))
    people, bad_people = _people(args.get("email"))
    types = [t for t in types if t not in trusted_sites][:MAX_ALLOWED]
    opens = [o for o in opens if o not in trusted_sites and o not in types][:MAX_ALLOWED]
    yes = lambda key: args.get(key) is True  # noqa: E731
    g = grants
    items = [(g.type_label(t), Grant(type={t})) for t in types]
    items += [(g.open_label(o), Grant(open={o})) for o in opens]
    items += [(g.SEARCH_LABEL, Grant(search=True))] if yes("search") else []
    items += [(g.email_label(p), Grant(email={p})) for p in people[:MAX_ALLOWED]]
    items += [(g.DRAFTS_LABEL, Grant(drafts=True))] if yes("drafts") else []
    items += [(g.CALENDAR_ADD_LABEL, Grant(calendar_add=True))] if yes("calendar_add") else []
    items += [(g.CALENDAR_CHANGE_LABEL, Grant(calendar_change=True))] if yes("calendar_change") else []
    return items, [*bad_opens, *bad_types, *bad_people]


def links_of(account: Account, task_id: str) -> list[str]:
    """The addresses a task's checks may open as given (SPEC §5.2 "Made-up addresses"): what the request that
    started it was shown, or the person wrote in it. A task kept before tasks kept them has none."""
    entry = next((e for e in load(account) if e.get("id") == task_id), None)
    return [u for u in (entry or {}).get("links", []) if isinstance(u, str)]


def clean_grant(data: object) -> Grant | None:
    """A grant the person edited in the Tasks panel (SPEC §7.4): sites by name, one address each; None when any
    isn't one."""
    if not isinstance(data, dict):
        return None
    g = Grant.from_dict(data)
    sites = {s: trusted.valid(s) for s in g.open | g.type}
    people = {p: emails_in(p) for p in g.email}
    if not all(sites.values()) or any(len(v) != 1 for v in people.values()):
        return None
    return Grant(
        {sites[s] for s in g.open},
        {sites[s] for s in g.type},
        g.search,
        {people[p][0] for p in g.email},
        g.drafts,
        g.calendar_add,
        g.calendar_change,
    )


def grant_of(account: Account, task_id: str) -> Grant:
    """What the person allowed up front when the task was started (SPEC §3.4): its checks run with it."""
    entry = next((e for e in load(account) if e.get("id") == task_id), None)
    return Grant.from_dict(entry.get("grant") if entry else None)


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


def _start(value, minutes: int, end: float | None, tz: ZoneInfo, now: float) -> tuple[float | None, str]:
    """The first check: a local time, or `every` from now when none is given."""
    text = str(value or "").strip()
    if not text:
        return now + minutes * 60, ""
    when = dates.parse_local(text, tz)
    if when is None:
        return None, "give `start` as a local date and time like 2026-11-30 08:00, or leave it out"
    if when.timestamp() < now - 60:
        return None, f"{dates.for_model(when)} has already passed: check the current time in the <context> block"
    if when.timestamp() > now + MAX_AHEAD:
        return None, "the first check is more than a year away"
    if end is not None and when.timestamp() >= end:
        return None, "the first check is after `until`"
    return max(when.timestamp(), now), ""


def _validate(title, prompt, every, until, tz: ZoneInfo, now: float, start=None):
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
    first, why = _start(start, minutes, end, tz, now)
    if first is None:
        return None, why
    return (title, prompt, minutes, end, first), ""


async def task_create(args: dict, ctx: ToolContext) -> ToolResult:
    tz, now = dates.tz(ctx), time.time()
    args_ = (args.get("title"), args.get("prompt"), args.get("every_minutes"), args.get("until"))
    got, why = _validate(*args_, tz, now, args.get("start"))
    if got is None:
        return ToolResult(f"Not created: {why}.", error=True)
    if len(load(ctx.account)) >= MAX_TASKS:
        return ToolResult(
            f"Not created: there are already {MAX_TASKS} background tasks: cancel some first.", error=True
        )
    title, prompt, minutes, end, first = got
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
    # What its checks may do without asking (SPEC §3.4 "Background tasks", §5.2 "Allowed up front"): the model
    # says, the person ticks on the card. A request the person watches asks for each action as it happens.
    items, bad = asked_items(args, set(trusted.sites(ctx.account)))
    if bad:
        return ToolResult(
            f"Not created: {', '.join(bad)} isn't a site's name (like fedex.com) or one email address. Fix it and "
            "call task_create again.",
            error=True,
        )
    # It checks later on its own, without the person watching, so they approve it (SPEC §3.4).
    fields = [
        Field("Waiting for", title),
        Field("How to check", prompt, "longtext"),
        Field("First check", dates.editable(dt.datetime.fromtimestamp(first, tz))),
        Field("Check every (minutes)", str(minutes)),
        Field("Until", _until_text(end, tz)),
    ]
    fields += [Field(label, "true", "bool") for label, _ in items]
    d, refused = await guard.send(ctx, "task", "Start this background task?", fields)
    if refused:
        return refused
    v = d.values
    got, why = _validate(v[0], v[1], v[3], v[4], tz, time.time(), v[2])
    if got is None:
        return ToolResult(
            f"The edited details weren't valid ({why}), so nothing was started. Ask the person.", error=True
        )
    title, prompt, minutes, end, first = got
    grant = Grant()
    for (_, g), ticked in zip(items, v[5:], strict=False):
        if ticked == "true":
            grant.add(g)
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
        "next": first,
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
        **({"grant": grant.to_dict()} if grant else {}),
    }
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
            "Sam emails', until 'never'): each check reports only what's new. For something at a time, once or "
            "repeating ('remind me at 6 to call Sam', 'in 5 minutes google my name', 'every morning at 8 "
            "summarise my email'): `start` at that time, `every_minutes` for the repeat (1440 for daily), and the "
            "check does it and answers DONE (once) or NOTIFY (repeating). Each check runs `prompt` as a new "
            "request, with only your memory: write it so it makes sense on its own later, with what to look at "
            "(the site by its name, like fedex.com, and how to find the page there; names, tracking codes), how, "
            "and what counts as done. Put an address in it only if you opened it or the user sent it in this "
            "request: never one you put together. Say what its checks will need to do without asking (`type` "
            "for typing the tracking number on fedex.com, `email` for who it writes to…): the user ticks each before "
            "it starts; anything not ticked waits for them when it happens. Don't say in it what to answer: "
            "each check is told how to answer. Checks run only while nobody is using Aha!, so timing isn't exact.",
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
                start=string(
                    "The first check, local YYYY-MM-DD HH:MM, for something at a time; leave it out to start "
                    "`every_minutes` from now. 'Every weekday at 8' asked on a Saturday evening starts Monday 08:00"
                ),
                type={**SITES, "description": "Sites its checks type on (a search box, a form), like fedex.com"},
                open={
                    **SITES,
                    "description": "Sites whose pages its checks only read, after reading the user's email or a "
                    "signed-in page (reading needs nothing otherwise)",
                },
                search={"type": "boolean", "description": "Its checks search the web after reading private things"},
                email={**SITES, "description": "Email addresses of the people its checks send email to or invite"},
                drafts={"type": "boolean", "description": "Its checks save email drafts (nothing is sent)"},
                calendar_add={"type": "boolean", "description": "Its checks add new events to the calendar"},
                calendar_change={"type": "boolean", "description": "Its checks change events already there"},
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
    """("waiting" | "notify" | "done" | "failed", the rest). An answer with no marker (after it was asked again) is
    sent as news and the task goes on: never closed, and never silently dropped."""
    found = OUTCOME.match(answer.strip())
    if found is None:
        return "notify", answer.strip()
    return found.group(1).lower(), answer.strip()[found.end() :].strip()


def marked(answer: str) -> bool:
    """Whether a check's answer starts with WAITING, NOTIFY, DONE or FAILED."""
    return OUTCOME.match(answer.strip()) is not None


def _last(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())[:MAX_LAST]


def finish(account: Account, task_id: str, answer: str, stopped: bool) -> Note | None:
    """After a check: its note for the chat when there's news, else None (it waits, quietly). WAITING and
    NOTIFY keep the task; DONE and FAILED end it. Whatever it was, the check's answer goes, whole, to the next
    check (SPEC §3.4): a WAITING line, a NOTIFY message, or what a check that didn't finish had."""
    kind, text = ("waiting", answer) if stopped else outcome(answer)
    if kind in ("waiting", "notify"):
        told = int(kind == "notify")
        how = "unfinished" if stopped else kind

        def checked(e: dict) -> dict:
            now = time.time()
            # A Run now check is an extra one: the planned next check keeps its time.
            later = e["next"] if e.get("manual") else now + e["every"] * 60
            return {
                **{k: v for k, v in e.items() if k not in ("now", "manual")},
                "last": _last(text),
                "last_how": how,
                "last_at": now,
                "next": later,
                "checks": e.get("checks", 0) + 1,
                "updates": e.get("updates", 0) + told,
            }

        entry = update(account, task_id, checked)
        if kind == "waiting" or entry is None:
            return None  # nothing to tell, or cancelled while it ran
    else:
        entry = update(account, task_id, lambda e: None)
        if entry is None:
            return None  # cancelled while it ran
        account.audit({"background_task_closed": task_id, "outcome": kind})
    return Note("update" if kind == "notify" else kind, entry["title"], text, entry["prompt"], entry["every"])


def interrupted(account: Account, task_id: str) -> None:
    """A check stopped by someone's input: the next one is told so (its result, whatever it was)."""
    update(
        account,
        task_id,
        lambda e: {
            **{k: v for k, v in e.items() if k != "manual"},
            "last": e.get("last", ""),
            "last_how": "interrupted",
            "last_at": time.time(),
        },
    )


EDITABLE = ("title", "prompt", "every", "until", "next", "grant")  # what the panel sends


def edit(account: Account, task_id: str, changes: dict) -> str:
    """The person's own edit of a task, any of its fields (the Tasks panel): "" when done, else why not.
    `every` in seconds; `until` and `next` epoch seconds (`until` None: no end); a `next` in the past is due now."""
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
    if "grant" in changes:
        grant = clean_grant(changes["grant"])
        if grant is None:
            return "a site or an email address isn't valid"
        new["grant"] = grant.to_dict()
    if "every" in changes:
        if not isinstance(changes["every"], int | float):
            return "how often must be a number"
        minutes, why = _every(changes["every"] / 60)
        if minutes is None:
            return why
        new["every"] = minutes
    if "until" in changes:
        until = changes["until"]
        if until is not None and (not isinstance(until, int | float) or until <= now + 60 or until > now + MAX_AHEAD):
            return "until must be in the future, within a year, or no end"
        new["until"] = until
    if "next" in changes:
        if not isinstance(changes["next"], int | float) or changes["next"] > now + MAX_AHEAD:
            return "the next check must be a time within a year"
        new["next"] = max(float(changes["next"]), now)
    if new["until"] is not None and new["next"] >= new["until"]:
        return "the next check is after until"
    kept = (*EDITABLE, "links")
    update(account, task_id, lambda e: {**e, **{k: new[k] for k in kept if k in new}})
    return ""


def _last_line(entry: dict, tz: ZoneInfo) -> str:
    how = entry.get("last_how", "waiting" if entry.get("last") else "")
    if not how:
        return ""
    when = dates.for_model(dt.datetime.fromtimestamp(entry.get("last_at") or entry["next"], tz))
    note = entry.get("last") or "(nothing)"
    return LAST_TIME.format(when=when, how=LAST_HOW.get(how, LAST_HOW["waiting"]), note=note)


def check_prompt(entry: dict, now: float, tz: ZoneInfo) -> str:
    late = now - entry["next"] > entry["every"] * 60
    return CHECK.format(
        n=entry.get("checks", 0) + 1,
        late=", later than planned: Aha! was busy" if late else "",
        title=entry["title"],
        prompt=entry["prompt"],
        last=_last_line(entry, tz),
        every=every_words(entry["every"]),
    )


def listing(account: Account) -> list[dict]:
    """For the app's Tasks panel, newest first (SPEC §6.5)."""
    keys = ("id", "title", "prompt", "until", "next", "created", "card")
    # "grant": what it allows, for the panel's editor; "allowed": the same in the person's words
    rows = sorted(load(account), key=lambda e: e.get("created", 0), reverse=True)
    return [
        {
            **{k: e.get(k) for k in keys},
            "every": e["every"] * 60,
            "last": e.get("last", ""),
            "checks": e.get("checks", 0),
            "updates": e.get("updates", 0),
            "now": bool(e.get("now")),
            "last_how": e.get("last_how", "waiting" if e.get("last") else ""),
            "allowed": Grant.from_dict(e.get("grant")).labels(),
            "grant": Grant.from_dict(e.get("grant")).to_dict(),
        }
        for e in rows
    ]


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
                elif (entry["next"] <= now or entry.get("now")) and not self.agent.is_running(account):
                    due.append((entry["next"], account, entry))
        asked = [d for d in due if d[2].get("now")]  # Run now: no idle wait, only a free model
        if self.agent.busy() or not (asked or (due and self.agent.idle(now))):
            return
        _, account, entry = min(asked or due, key=lambda d: d[0])
        manual = bool(entry.get("now"))  # an extra check: it leaves the planned next one where it is
        await self.agent.start_background(account, entry["id"], check_prompt(entry, now, self.tz))
        update(account, entry["id"], lambda e: {**{k: v for k, v in e.items() if k != "now"}, "manual": manual})

    async def _expire(self, account: Account, entry: dict) -> None:
        if update(account, entry["id"], lambda e: None) is None:
            return
        account.audit({"background_task_closed": entry["id"], "outcome": "expired"})
        when = dates.for_model(dt.datetime.fromtimestamp(entry["until"], self.tz))
        last = f" The last check found: {entry['last']}" if entry.get("last") else ""
        text = EXPIRED.format(when=when, last=last)
        await self.agent.task_note(account, Note("expired", entry["title"], text, entry["prompt"], entry["every"]))
