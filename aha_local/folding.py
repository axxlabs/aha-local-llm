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
    "[Aha! app, not from the user. Decide whether the user's latest message starts a new subject, then give a "
    "title. Their latest message was: “{asked}”.\n"
    "NEW if it is about a different thing from the conversation before it since {since}: another person, place, "
    "company, product, booking, order, email, trip, task or question. It is NEW even when it is the same kind of "
    "request as before: two “who is …” questions about different people are two subjects, and so are two "
    "searches for different things. It is also NEW when the user says they're changing the subject.\n"
    "SAME only if it continues the same thing: a follow-up or detail about it, a correction, an answer to your "
    "question, or a message that only makes sense with what came before (“and the price?”, “book it”, “make it "
    "8pm”, “what about the other one?”).\n"
    "Reply with one line only: NEW: and a title for the latest message's request, or SAME: and a title for "
    "everything you talked about since {since}. A title is at most 6 words, in the language of the conversation, "
    "no quotes, no full stop.]"
)
# The person says so: a new part whatever the model answers (seen: "ok, let's change topics. What happened to …"
# came back SAME).
SAYS_NEW = re.compile(
    r"\b(?:chang\w*|switch\w*|new|different|another|other|next)\s+(?:the\s+)?(?:topics?|subjects?)\b|"
    r"\b(?:topic|subject)\s+change\b|\b(?:something|anything)\s+(?:else|different)\b|\bmoving on\b|"
    r"\bon (?:a|another) note\b|\bunrelated\b|"
    r"\b(?:mud\w*|troc\w*)\s+de\s+(?:assunto|tema)\b|\b(?:outro|novo)\s+(?:assunto|tema)\b",
    re.I,
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


def ask(since: str, asked: str = "") -> str:
    return ASK.format(since=since, asked=" ".join(asked.split())[:300])


def says_new(asked: str) -> bool:
    """The person said they're changing the subject."""
    return bool(SAYS_NEW.search(asked or ""))


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
