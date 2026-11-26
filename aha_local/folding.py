# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topics (SPEC §7.4 "Folding"): where the topic going on starts, its title, and whether its last answer waits
for the person.

The model says, at the end of each request, whether the person's message started a new subject and whether its
answer waits for their reply; the person's [collapse] starts one too. The app folds its view after a break in
time on its own; that doesn't start a topic here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SHOWN = ("msg", "approval", "steps", "act")  # what the chat shows; states and titles aren't part of a topic
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
    "First line: NEW: and a title for the latest message's request, or SAME: and a title for everything you "
    "talked about since {since}. A title is at most 6 words, in the language of the conversation, no quotes, no "
    "full stop.\n"
    "Second line: WAITING: yes if your last answer needs the user's reply before anything can go on (a question "
    "you asked them, a choice for them to make), else WAITING: no.\n"
    "Then a line starting MEMORY: and, on the lines after it, your memory of this topic (NEW: the latest request "
    "only; SAME: everything since {since}), replacing what you wrote before for it. Later, when these messages "
    "are gone, this is all you'll have in view about it, so keep what matters: what was asked, what was done or "
    "found, and the exact details: tracking and reference numbers, order and booking codes, email and postal "
    "addresses, names, phone numbers, dates (DD/MM/YYYY) and times, amounts, links, and what's still open or "
    "promised. Only what came up here, from the user or your tools; never a password, one-time code or card "
    "number. Short plain lines, at most 150 words. If nothing is worth keeping, write MEMORY: none]"
)
NEW = re.compile(
    r"\b(?:chang\w*|switch\w*|new|different|another|other|next)\s+(?:the\s+)?(?:topics?|subjects?)\b|"
    r"\b(?:topic|subject)\s+change\b|\b(?:something|anything)\s+(?:else|different)\b|\bmoving on\b|"
    r"\bon (?:a|another) note\b|\bunrelated\b|"
    r"\b(?:mud\w*|troc\w*)\s+de\s+(?:assunto|tema)\b|\b(?:outro|novo)\s+(?:assunto|tema)\b",
    re.I,
)
NEW = re.compile(r"^\W*(NEW|SAME)\W*[:\-]\s*", re.I)
WAITING = re.compile(r"^\W*WAITING\W*[:\-]\s*\W*yes\b", re.I)


def segment_start(items: list[dict]) -> tuple[int, int] | None:
    """(seq, ts) of the first item of the topic going on (SPEC §7.4 "Folding"): since the last new subject the model
    marked, or the last cut (the person's [collapse]). A break in time only folds the app's view. `items`: the
    latest chat items, oldest first (Account.recent_items)."""
    cuts = [it["seq"] for it in items if it.get("t") == "cut"]
    shown = [(it["seq"], it["ts"]) for it in items if it.get("t") in SHOWN and it["seq"] > max(cuts, default=0)]
    if not shown:
        return None
    topics = {it.get("from") for it in items if it.get("t") == "title" and it.get("new")}
    later = [s for s in shown if s[0] in topics]
    return later[-1] if later else shown[0]


def ask(since: str, asked: str = "") -> str:
    return ASK.format(since=since, asked=" ".join(asked.split())[:300])


MEMORY = re.compile(r"^[\W_]*MEMORY[ \t*_]*[:\-][ \t*_]*", re.I | re.M)


@dataclass(frozen=True)
class Topic:
    """The model's reply to ASK."""

    new: bool  # a new subject starts at the person's latest message
    title: str
    memory: str  # the topic's memory entry, "" for none
    waiting: bool  # the last answer waits for the person's reply: its part stays open (SPEC §7.4)


def parse(text: str) -> Topic:
    parts = MEMORY.split(text.strip(), maxsplit=1)
    head, memory = (parts[0], parts[1]) if len(parts) == 2 else (text, "")
    lines = [x for x in head.strip().splitlines() if x.strip()]
    line = lines[0] if lines else ""
    m = NEW.match(line)
    waiting = any(WAITING.match(x) for x in lines[1:])
    memory = memory.strip()
    if memory.lower().rstrip(".") in ("none", "nothing", "n/a"):
        memory = ""
    return Topic(bool(m and m.group(1).upper() == "NEW"), clean(line[m.end() :] if m else line), memory, waiting)


def clean(text: str) -> str:
    """The model's title as plain text, or "" if it isn't one (the app then uses the first words)."""
    line = next((x for x in text.strip().splitlines() if x.strip()), "")
    line = re.sub(r"^(?:title|titel|título|titre)\s*:\s*", "", line.strip(), flags=re.I)
    line = line.strip(" \t\"'“”‘’`*_#").rstrip(".").strip()
    if not line or len(line.split()) > 10 or len(line) > MAX_TITLE or "[" in line:
        return ""
    return line
