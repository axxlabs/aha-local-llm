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
    "[Aha! app, not from the user. Reply with one line only. If the user's latest request is about a subject "
    "unrelated to what you talked about before it since {since}, reply NEW: and a title for that latest request. "
    "Otherwise reply SAME: and a title for everything you talked about since {since}. A title is at most 6 words, "
    "in the language of the conversation, no quotes, no full stop.]"
)
NEW = re.compile(r"^\W*(NEW|SAME)\W*[:\-]\s*", re.I)


def is_break(before: int, after: int, tz: ZoneInfo) -> bool:
    day = lambda ts: dt.datetime.fromtimestamp(ts, tz).date()  # noqa: E731
    return after - before >= BREAK_SECONDS or day(before) != day(after)


def segment_start(transcript: list[dict], tz: ZoneInfo) -> tuple[int, int] | None:
    """(seq, ts) of the first item of the current part: since the last break, or since the last new subject."""
    items = [r["item"] for r in transcript if r.get("kind") == "item" and isinstance(r.get("item"), dict)]
    shown = [(it["seq"], it["ts"]) for it in items if it.get("t") in SHOWN]
    if not shown:
        return None
    start = len(shown) - 1
    while start > 0 and not is_break(shown[start - 1][1], shown[start][1], tz):
        start -= 1
    topics = {it.get("from") for it in items if it.get("t") == "title" and it.get("new")}
    later = [s for s in shown[start:] if s[0] in topics]
    return later[-1] if later else shown[start]


def asks(answer: str) -> bool:
    """The answer ends by asking the person something: it stays open until they reply."""
    tail = re.split(r"\n\s*\n", answer.strip())[-1]
    return "?" in tail[-300:]


def ask(since: str) -> str:
    return ASK.format(since=since)


def parse(text: str) -> tuple[bool, str]:
    """(a new subject?, the title) from the model's reply to ASK."""
    line = next((x for x in text.strip().splitlines() if x.strip()), "")
    m = NEW.match(line)
    return bool(m and m.group(1).upper() == "NEW"), clean(line[m.end() :] if m else line)


def clean(text: str) -> str:
    """The model's title as plain text, or "" if it isn't one (the app then uses the first words)."""
    line = next((x for x in text.strip().splitlines() if x.strip()), "")
    line = re.sub(r"^(?:title|titel|título|titre)\s*:\s*", "", line.strip(), flags=re.I)
    line = line.strip(" \t\"'“”‘’`*_#").rstrip(".").strip()
    if not line or len(line.split()) > 10 or len(line) > MAX_TITLE or "[" in line:
        return ""
    return line
