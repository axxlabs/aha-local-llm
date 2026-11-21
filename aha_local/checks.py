# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Checks on the model's final answer, in code (SPEC §3.5, "numbers come from sources").

1. chat_format: Markdown the app doesn't render (headings) is normalised; everything else is left as Markdown.
2. unverified: times, prices, dates, phone numbers and reference-like numbers in the answer that appear in no
   page aha opened during the task (fetch_url or the browser), in the person's own messages, in their memories
   or in emails they let it read. Search-result snippets don't count. Those numbers get a visible warning, so a
   made-up number can't pass as checked.
"""

from __future__ import annotations

import re

HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$", re.M)


def chat_format(text: str) -> str:
    """The app renders a safe subset of Markdown (SPEC §7.4); headings become bold lines."""
    text = HEADING.sub(r"**\1**", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# What counts as a "number that must come from a source", with the fewest digits each must have.
PATTERNS = [
    (re.compile(r"(?<![\d:])(?:[01]?\d|2[0-3])[:.][0-5]\d(?![\d:])"), 3),  # times 10:00, 9.30
    (re.compile(r"(?<![\w:.])\d{1,2}(?:[:.]\d\d)?\s?(?:am|pm)\b", re.I), 1),  # 10am, 5 pm (not "00 AM" of 10:00 AM)
    (re.compile(r"[£$€]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:GBP|USD|EUR)\b"), 1),  # prices
    (re.compile(r"(?<![\d+])(?:\+\d{1,3}[\s-]?)?(?:\(?\d{2,5}\)?[\s-]?){2,4}\d{3,4}(?!\d)"), 7),  # phone-like
]
DIGITS = re.compile(r"\d")


def _norm(s: str) -> str:
    """Compare numbers loosely: 10.00 = 10:00, '£ 212' = '£212', '07700 900123' = '07700900123'."""
    s = s.lower().replace(".", ":").replace(",", "")
    return re.sub(r"[\s()-]", "", s)


def numbers_in(text: str) -> list[str]:
    found, seen = [], set()
    for pattern, min_digits in PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(0).strip()
            if len(DIGITS.findall(value)) < min_digits:
                continue
            key = _norm(value)
            if key not in seen:
                seen.add(key)
                found.append(value)
    return found


def _variants(value: str) -> set[str]:
    n = _norm(value)
    out = {n}
    m = re.fullmatch(r"(\d{1,2})(?::(\d\d))?(am|pm)", n)
    if m:  # 10am ↔ 10:00, 5:30pm ↔ 17:30
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        out |= {f"{h}:{m.group(2) or '00'}", f"{h:02d}:{m.group(2) or '00'}"}
    m = re.fullmatch(r"(\d{1,2}):(\d\d)", n)
    if m:  # 17:00 ↔ 5pm, 10:00 ↔ 10am
        h, mm = int(m.group(1)), m.group(2)
        suffix = "pm" if h >= 12 else "am"
        h12 = h % 12 or 12
        out |= {f"{h12}{suffix}" if mm == "00" else f"{h12}:{mm}{suffix}", f"{h12}:{mm}{suffix}", f"{h:02d}:{mm}"}
    return out


def unverified(answer: str, sources: list[str]) -> list[str]:
    haystack = _norm(" ".join(sources))
    return [v for v in numbers_in(answer) if not any(x in haystack for x in _variants(v))]


WARNING_MARK = "⚠️ _Not confirmed on a page I opened"


MEMORY_REQUEST = re.compile(r"\b(forget|remember|memor\w*|save|saving|delete|remove|erase|update|change)\b", re.I)
# Affirmative only: "I've forgotten", "Done, I deleted", "I'll no longer remember". "I haven't saved" and
# "I never saved" don't match, because the verb must follow "I", "I've", "I have" directly.
MEMORY_CLAIM = re.compile(
    r"\b(?:I|I've|I have|I've now|I have now|I just)\s+(?:forgot|forgotten|removed|deleted|erased|saved|"
    r"remembered|updated|stored|noted)\b|\bI(?:'ll| will) no longer remember\b",
    re.I,
)
MEMORY_WRITE_TOOLS = {"memory_add", "memory_update", "memory_delete"}


def unbacked_memory_claim(asked: str, answer: str, tools_called: set[str]) -> bool:
    """The person asked about their memories, the answer says it's done, and no memory tool ran in this task.

    Memories change only through the tools (and the person's approval), so such an answer is untrue.
    """
    return bool(MEMORY_REQUEST.search(asked) and MEMORY_CLAIM.search(answer) and not tools_called & MEMORY_WRITE_TOOLS)


MEMORY_RETRY = (
    "[aha check] Your draft says a memory was saved, changed or forgotten, but you haven't called memory_add, "
    "memory_update or memory_delete in this task, so nothing changed. Memories are listed in the <context> block. "
    "If the person wants a change, call the tool now (they'll approve it); if not, correct your answer. "
    "Then write your reply to the person from scratch: they haven't seen your draft, and never mention this check."
)


SIGNIN_CLAIM = re.compile(
    r"\bsign[- ]?in card\b|\bcard (?:that )?(?:has )?appeared\b|\bsign in (?:on|using|with) the card\b", re.I
)


def unbacked_signin_claim(answer: str, tools_called: set[str]) -> bool:
    """The answer points the person at a sign-in card, but browser_sign_in didn't run: there's no card."""
    return bool(SIGNIN_CLAIM.search(answer)) and "browser_sign_in" not in tools_called


SIGNIN_RETRY = (
    "[aha check] Your draft tells the person to use a sign-in card, but you haven't called browser_sign_in in this "
    "task, so no card was shown. A card appears only when you call browser_sign_in yourself, with the references "
    "of the sign-in fields from the latest snapshot (take one if needed; if the page has only an email field "
    "first, use that alone). If you can't find the fields, say so plainly. Then write your reply to the person "
    "from scratch: they haven't seen your draft, and never mention this check."
)


def retry_prompt(values: list[str]) -> str:
    shown = ", ".join(values[:8])
    return (
        f"[aha check] Your draft answer contains numbers that aren't on any page you opened in this task: {shown}. "
        "Search snippets don't count. Open the page that shows them (fetch_url, or the browser if the site "
        "blocks it) and confirm them, or give the answer without them and say you couldn't confirm them. "
        "Then write your reply to the person from scratch: they haven't seen your draft, and never mention this check."
    )


def warning(values: list[str]) -> str:
    shown = ", ".join(values[:5]) + ("…" if len(values) > 5 else "")
    return f"{WARNING_MARK}: {shown}. Please check before relying on it._"
