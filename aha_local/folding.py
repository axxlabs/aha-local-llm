# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Folding the chat after a break (SPEC §7.4 "Folding"): where the current part of the conversation starts, its
title, and which of Aha!'s messages stay open.

The app does the folding itself, from the same rule; the computer only names the parts and marks what's
unfinished, so every device folds the same way.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

BREAK_SECONDS = 2 * 3600  # a break: two hours without a message, or a new day (app: src/fold.ts)
SHOWN = ("msg", "approval")  # what the chat shows; states and titles don't count for breaks
MAX_TITLE = 60
ASK = (
    "[Aha! app, not from the user: in at most 6 words, in the language of the conversation, give a title for "
    "what you and the user talked about since {since}. Reply with the title only: no quotes, no full stop.]"
)


def is_break(before: int, after: int, tz: ZoneInfo) -> bool:
    day = lambda ts: dt.datetime.fromtimestamp(ts, tz).date()  # noqa: E731
    return after - before >= BREAK_SECONDS or day(before) != day(after)


def segment_start(transcript: list[dict], tz: ZoneInfo) -> tuple[int, int] | None:
    """(seq, ts) of the first item since the last break, from the items the devices were sent."""
    shown = [
        (r["item"]["seq"], r["item"]["ts"])
        for r in transcript
        if r.get("kind") == "item" and isinstance(r.get("item"), dict) and r["item"].get("t") in SHOWN
    ]
    if not shown:
        return None
    start = len(shown) - 1
    while start > 0 and not is_break(shown[start - 1][1], shown[start][1], tz):
        start -= 1
    return shown[start]


def asks(answer: str) -> bool:
    """The answer ends by asking the person something: it stays open until they reply."""
    tail = re.split(r"\n\s*\n", answer.strip())[-1]
    return "?" in tail[-300:]


def ask(since: str) -> str:
    return ASK.format(since=since)


def clean(text: str) -> str:
    """The model's title as plain text, or "" if it isn't one (the app then uses the first words)."""
    line = next((x for x in text.strip().splitlines() if x.strip()), "")
    line = re.sub(r"^(?:title|titel|título|titre)\s*:\s*", "", line.strip(), flags=re.I)
    line = line.strip(" \t\"'“”‘’`*_#").rstrip(".").strip()
    if not line or len(line.split()) > 10 or len(line) > MAX_TITLE or "[" in line:
        return ""
    return line
