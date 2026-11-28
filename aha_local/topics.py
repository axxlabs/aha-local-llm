# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topics (SPEC §4.1 "Topics that wait", §7.4): the numbered threads of a person's chat, kept in `topics.json`.

Every chat item belongs to one topic (its `topic`, a number from 1, per person, never reused). A topic is opened by
the person (typing with it open in the app) or started by a message typed with every topic collapsed; sha can
send it to the background (a schedule: it wakes up on its own, inside the topic). This file is the one place a
topic's title, state and schedule live:

    {"next": N, "topics": {"<n>": {"n", "title", "first", "created", "last", "state", "question"?,
                                    "allowed"?, "schedule"?}}}

`state`: "" (a plain topic), "scheduled" (it wakes on its own), "waiting" (it waits on the person: a question or
a card), "done" or "failed" (its schedule ended). The app is told every change with a `topic` item.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar

FILE = "topics.json"
STATES = ("", "scheduled", "waiting", "done", "failed")
MAX_TITLE = 60
MAX_QUESTION = 200
ACTIVE = ("scheduled", "waiting")

# The topic a run works in: every chat item published meanwhile belongs to it (SPEC §4.1).
TOPIC: ContextVar[int | None] = ContextVar("aha_topic", default=None)
# Told when a topic's title, state or schedule changes: the channel sends the app a `topic` item (SPEC §6.5).
WATCHERS: list[Callable[[object, dict], None]] = []


def load(account) -> dict:
    data = account.vault.read_json(account.dir / FILE, None) or {}
    return {"next": int(data.get("next", 1)), "topics": dict(data.get("topics") or {})}


def save(account, data: dict) -> None:
    account.vault.write_json(account.dir / FILE, data)


def get(account, n: int | None) -> dict | None:
    if not isinstance(n, int):
        return None
    return load(account)["topics"].get(str(n))


def all_topics(account) -> list[dict]:
    return sorted(load(account)["topics"].values(), key=lambda e: e["n"])


def new(account, first: int = 0, title: str = "", now: float | None = None) -> int:
    """A new topic, its number given now."""
    data = load(account)
    n = data["next"]
    ts = int(time.time() if now is None else now)
    entry = {"n": n, "title": title[:MAX_TITLE], "first": first, "created": ts, "last": ts, "state": ""}
    data["topics"][str(n)] = entry
    data["next"] = n + 1
    save(account, data)
    for watcher in WATCHERS:
        watcher(account, entry)
    return n


def update(account, n: int, fn: Callable[[dict], dict], quiet: bool = False) -> dict | None:
    """Change one topic, with no await between read and write. Returns it as it is now, or None if there's none.
    The app is told, unless `quiet` (only its last activity changed, which the app sees in its items)."""
    data = load(account)
    entry = data["topics"].get(str(n))
    if entry is None:
        return None
    before = dict(entry)
    entry = fn(dict(entry))
    data["topics"][str(n)] = entry
    save(account, data)
    if not quiet and {k: v for k, v in entry.items() if k != "last"} != {
        k: v for k, v in before.items() if k != "last"
    }:
        for watcher in WATCHERS:
            watcher(account, entry)
    return entry


def route(account, wanted: object, first: int = 0) -> int:
    """The topic a message the person typed goes to: the one open in their app (`wanted`), or a new one when every
    topic was collapsed (SPEC §4.1 "Routing")."""
    if isinstance(wanted, int) and not isinstance(wanted, bool) and get(account, wanted) is not None:
        return wanted
    return new(account, first)


def touch(account, n: int, ts: float | None = None) -> None:
    """Something happened in topic n: it moves to the end of its day in the app (SPEC §4.1 "Order and groups")."""
    stamp = int(time.time() if ts is None else ts)
    update(account, n, lambda e: {**e, "last": stamp}, quiet=True)


def set_title(account, n: int, title: str) -> dict | None:
    title = " ".join(title.split())[:MAX_TITLE]
    return update(account, n, lambda e: {**e, "title": title}) if title else get(account, n)


def wait_on_person(account, n: int, question: str) -> dict | None:
    """Its last answer needs the person's reply: it shows under Needs input, with the question's first line."""
    first = next((x.strip() for x in question.strip().splitlines() if x.strip()), "")[:MAX_QUESTION]
    return update(account, n, lambda e: {**e, "state": "waiting", "question": first})


def answered(account, n: int) -> dict | None:
    """The person wrote in it: it no longer waits on them (scheduled again, if it has a schedule)."""

    def fn(e: dict) -> dict:
        e = {k: v for k, v in e.items() if k != "question"}
        if e.get("state") in ("waiting", "done", "failed"):
            e["state"] = "scheduled" if e.get("schedule") else ""
        return e

    return update(account, n, fn)


def clear(account) -> None:
    """Every topic goes, and whatever they were checking on (Clear history, SPEC §3.8.1); numbers carry on."""
    save(account, {"next": load(account)["next"], "topics": {}})


def remove(account, n: int) -> dict | None:
    """Topic n goes from the list, with its schedule (the person's Delete topic). Returns it as it was."""
    data = load(account)
    gone = data["topics"].pop(str(n), None)
    if gone is not None:
        save(account, data)
    return gone


def active(account) -> list[dict]:
    """The topics that go on without the person in them: scheduled, or waiting on them."""
    return [e for e in all_topics(account) if e.get("state") in ACTIVE]


def for_app(entry: dict) -> dict:
    """A topic as the app's `topic` item carries it (SPEC §6.5): its state and, if scheduled, when it wakes."""
    out = {
        "topic": entry["n"],
        "title": entry.get("title", ""),
        "state": entry.get("state", ""),
        "last": entry.get("last", 0),
    }
    if entry.get("question"):
        out["question"] = entry["question"]
    s = entry.get("schedule")
    if s:
        out["schedule"] = {
            "waiting_for": s.get("title", ""),
            "prompt": s.get("prompt", ""),
            "done_when": s.get("done_when", ""),
            "every": s.get("every", 0) * 60,
            **({"at": s["at"]} if s.get("at") else {}),
            "next": s.get("next", 0),
            "checks": s.get("checks", 0),
            "paused": bool(s.get("paused")),
        }
    return out


def context_lines(account) -> list[str]:
    """The active topics, for the model's <context> (SPEC §4.1 "Context")."""
    out = []
    for e in active(account):
        what = "waiting on the user" if e["state"] == "waiting" else "checking on its own"
        out.append(f"- topic {e['n']}: {e.get('title') or '(untitled)'} ({what})")
    return out
