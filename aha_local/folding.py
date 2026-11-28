# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""A topic's title, memory entry, and whether its last answer waits for the person (SPEC §4.1, §7.4).

Which topic a message goes to is the person's choice in the app (the topic open, or a new one), never the
model's; at the end of each request the model only names the topic, writes its memory and says whether its
answer waits for the person's reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SHOWN = ("msg", "approval", "steps", "act")  # what the chat shows: each belongs to a topic
MAX_TITLE = 60
ASK = (
    "[sha app, not from the user. Name this topic and keep your memory of it. Their latest message was: "
    "“{asked}”.\n"
    "First line: TITLE: a title for everything talked about in this topic, at most 6 words, in the language of the "
    "conversation, no quotes, no full stop.\n"
    "Second line: WAITING: yes if your last answer needs the user's reply before anything can go on (a question "
    "you asked them, a choice for them to make), else WAITING: no.\n"
    "Then a line starting MEMORY: and, on the lines after it, your memory of this topic, replacing what you wrote "
    "before for it. Later, when these messages are gone, this is all you'll have in view about it, so keep what "
    "matters: what was asked, what was done or found, and the exact details: tracking and reference numbers, order "
    "and booking codes, email and postal addresses, names, phone numbers, dates (DD/MM/YYYY) and times, amounts, "
    "links, and what's still open or promised. Only what came up here, from the user or your tools; never a "
    "password, one-time code or card number. Short plain lines, at most 150 words. If nothing is worth keeping, "
    "write MEMORY: none]"
)
TITLE = re.compile(r"^\W*TITLE\W*[:\-]\s*", re.I)
WAITING = re.compile(r"^\W*WAITING\W*[:\-]\s*\W*yes\b", re.I)
MEMORY = re.compile(r"^[\W_]*MEMORY[ \t*_]*[:\-][ \t*_]*", re.I | re.M)


def ask(asked: str = "") -> str:
    return ASK.format(asked=" ".join(asked.split())[:300])


@dataclass(frozen=True)
class Topic:
    """The model's reply to ASK."""

    title: str
    memory: str  # the topic's memory entry, "" for none
    waiting: bool  # the last answer waits for the person's reply: the topic shows under Needs input (SPEC §4.1)


def parse(text: str) -> Topic:
    parts = MEMORY.split(text.strip(), maxsplit=1)
    head, memory = (parts[0], parts[1]) if len(parts) == 2 else (text, "")
    lines = [x for x in head.strip().splitlines() if x.strip()]
    line = lines[0] if lines else ""
    m = TITLE.match(line)
    waiting = any(WAITING.match(x) for x in lines[1:])
    memory = memory.strip()
    if memory.lower().rstrip(".") in ("none", "nothing", "n/a"):
        memory = ""
    return Topic(clean(line[m.end() :] if m else line), memory, waiting)


def clean(text: str) -> str:
    """The model's title as plain text, or "" if it isn't one (the app then uses the first words)."""
    line = next((x for x in text.strip().splitlines() if x.strip()), "")
    line = re.sub(r"^(?:title|titel|título|titre)\s*:\s*", "", line.strip(), flags=re.I)
    line = line.strip(" \t\"'“”‘’`*_#").rstrip(".").strip()
    if not line or len(line.split()) > 10 or len(line) > MAX_TITLE or "[" in line:
        return ""
    return line
