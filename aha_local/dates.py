# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Dates and times, in one place (SPEC §3.5 "Dates", §3.4).

- The household's time zone (`tz`, `now`).
- What tools take, and cards show for editing: `YYYY-MM-DD HH:MM`, or `YYYY-MM-DD` for a whole day (`parse_local`,
  `when_of`, `editable`).
- What the model reads and writes: DD/MM/YYYY, never a weekday it worked out itself (`say`, `for_model`); the
  app shows each date with its weekday. A card may say the day in words (`in_words`), worked out here.
- The days a message names, worked out by code (`note`).

Seen: "Book … Tuesday, 2pm, for 2 people", asked on Tuesday 24 November, was booked for "Tuesday 2 December"
(a Wednesday): the model mixed the weekday with the 2 from "2pm". The list of coming days in the context wasn't
enough, so the dates a message names are put right after it, worked out here.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

WEEKDAYS = {
    0: ("monday", "mon", "segunda", "segunda-feira", "seg"),
    1: ("tuesday", "tue", "tues", "terça", "terca", "terça-feira", "terca-feira", "ter"),
    2: ("wednesday", "wed", "quarta", "quarta-feira", "qua"),
    3: ("thursday", "thu", "thur", "thurs", "quinta", "quinta-feira", "qui"),
    4: ("friday", "fri", "sexta", "sexta-feira", "sex"),
    5: ("saturday", "sat", "sábado", "sabado", "sáb", "sab"),
    6: ("sunday", "sun", "domingo", "dom"),
}
# Short forms that are ordinary words too ("sun", "sat", "sex", "ter", "dom", "qua", "seg") count only with a
# capital letter or a dot, as a day is usually written.
AMBIGUOUS = {"sun", "sat", "sex", "ter", "dom", "qua", "seg", "qui", "wed", "sab", "mon"}
MONTHS = {
    1: ("january", "jan", "janeiro"),
    2: ("february", "feb", "fevereiro", "fev"),
    3: ("march", "mar", "março", "marco"),
    4: ("april", "apr", "abril", "abr"),
    5: ("may", "maio", "mai"),
    6: ("june", "jun", "junho"),
    7: ("july", "jul", "julho"),
    8: ("august", "aug", "agosto", "ago"),
    9: ("september", "sep", "sept", "setembro"),  # not "set", "out": "2 out of 5"
    10: ("october", "oct", "outubro"),
    11: ("november", "nov", "novembro"),
    12: ("december", "dec", "dezembro", "dez"),
}
_DAY_WORD = {w: d for d, words in WEEKDAYS.items() for w in words}
_MONTH_WORD = {w: m for m, words in MONTHS.items() for w in words}
_WORD = r"[A-Za-zÀ-ÿ]+(?:-feira)?"
_MONTHS_RE = "|".join(sorted(_MONTH_WORD, key=len, reverse=True))
DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|º)?\s+(?:de\s+|of\s+)?({_MONTHS_RE})\b\.?", re.I)
MONTH_DAY = re.compile(rf"\b({_MONTHS_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*(?:am|pm|h\b|:))", re.I)
ORDINAL = re.compile(r"\b(?:the\s+|dia\s+)(\d{1,2})(?:st|nd|rd|th|º)?\b|\b(\d{1,2})(?:st|nd|rd|th)\b", re.I)
TODAY = re.compile(r"\b(today|tonight|hoje)\b", re.I)
TOMORROW = re.compile(r"\b(tomorrow|amanhã|amanha)\b", re.I)
WEEKEND = re.compile(r"\b(this|next)?\s*weekend\b|\b(este|próximo|proximo)?\s*fim de semana\b", re.I)
NEXT_WEEK = re.compile(r"\bnext week\b|\bpróxima semana\b|\bproxima semana\b|\bsemana que vem\b", re.I)
AFTER_TOMORROW = re.compile(r"\b(day after tomorrow|depois de amanhã|depois de amanha)\b", re.I)


INPUT = "%Y-%m-%d %H:%M"


def tz(config_or_name) -> ZoneInfo:
    """The household's time zone: from a name, a config, or anything with a config (a tool's context)."""
    if isinstance(config_or_name, str):
        return ZoneInfo(config_or_name)
    config = getattr(config_or_name, "config", config_or_name)
    return ZoneInfo(config.timezone)


def now(config_or_name) -> dt.datetime:
    return dt.datetime.now(tz(config_or_name))


def parse_local(text: str, zone: ZoneInfo) -> dt.datetime | None:
    """'2026-11-22 09:00' (or with a T, or an offset) → an aware local datetime."""
    try:
        when = dt.datetime.fromisoformat(str(text).strip().replace("/", "-"))
    except ValueError:
        return None
    return when.replace(tzinfo=zone) if when.tzinfo is None else when.astimezone(zone)


def when_of(value, zone: ZoneInfo) -> dt.datetime | dt.date | None:
    """'2026-11-24 19:00' → a local time; '2026-11-24' → a whole day."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            return None
    return parse_local(text, zone)


def editable(value: dt.datetime | dt.date) -> str:
    """As tools take it and cards show it, so an edit on the card reads back the same way."""
    return value.strftime(INPUT) if isinstance(value, dt.datetime) else value.isoformat()


def for_model(when: dt.datetime) -> str:
    """A time for the model: DD/MM/YYYY HH:MM, no weekday (the app labels the date with its own)."""
    return when.strftime("%d/%m/%Y %H:%M")


def in_words(day: str) -> str:
    """'2026-12-02…' → 'Wednesday 02 December 2026', for a card, so the person sees at once if it's the wrong day;
    "?" when it isn't a date."""
    try:
        return dt.date.fromisoformat(day.strip()[:10]).strftime("%A %d %B %Y")
    except ValueError:
        return "?"


def say(d: dt.date) -> str:
    """A full date, DD/MM/YYYY: how the model writes every date (the app shows its weekday)."""
    return d.strftime("%d/%m/%Y")


def _weekday(word: str) -> int | None:
    bare = word.rstrip(".")
    low = bare.lower()
    if low not in _DAY_WORD:
        return None
    if low in AMBIGUOUS and not (bare[0].isupper() or word.endswith(".")):
        return None
    return _DAY_WORD[low]


def _next_day_of_month(today: dt.date, day: int, month: int | None = None) -> dt.date | None:
    """The next date (today included) with that day of the month (and month, when given)."""
    for k in range(0, 400):
        d = today + dt.timedelta(days=k)
        if d.day == day and (month is None or d.month == month):
            return d
    return None


def note(text: str, today: dt.date) -> str:
    """The dates the message names, as a line to put after it; "" when it names none."""
    found: dict[str, str] = {}
    if AFTER_TOMORROW.search(text):
        found[AFTER_TOMORROW.search(text).group(1)] = say(today + dt.timedelta(days=2))
    elif m := TOMORROW.search(text):
        found[m.group(1)] = say(today + dt.timedelta(days=1))
    if m := TODAY.search(text):
        found[m.group(1)] = say(today)
    for word in re.findall(_WORD + r"\.?", text):
        wd = _weekday(word)
        if wd is None or word.rstrip(".") in found:
            continue
        ahead = (wd - today.weekday()) % 7
        first = today + dt.timedelta(days=ahead or 7)
        key = word.rstrip(".")
        before = text[: text.find(word)].lower().rstrip()
        if ahead == 0:
            found[key] = f"{say(today)} (today), or {say(first)} (the next one)"
        elif before.endswith(("next", "próxima", "proxima", "próximo", "proximo")):
            found[f"next {key}"] = f"{say(first)}, or {say(first + dt.timedelta(days=7))} if they mean the week after"
        else:
            found[key] = f"{say(first)} (the one after: {say(first + dt.timedelta(days=7))})"
    for m in DAY_MONTH.finditer(text):
        d = _next_day_of_month(today, int(m.group(1)), _MONTH_WORD[m.group(2).lower()])
        if d:
            found[m.group(0).strip()] = say(d)
    for m in MONTH_DAY.finditer(text):
        d = _next_day_of_month(today, int(m.group(2)), _MONTH_WORD[m.group(1).lower()])
        if d:
            found[m.group(0).strip()] = say(d)
    for m in ORDINAL.finditer(text):
        if any(m.group(0).strip() in k or k in m.group(0) for k in found):
            continue
        d = _next_day_of_month(today, int(m.group(1) or m.group(2)))
        if d:
            found[m.group(0).strip()] = say(d)
    if m := WEEKEND.search(text):
        sat = today + dt.timedelta(days=(5 - today.weekday()) % 7)
        if (m.group(1) or m.group(2) or "").lower().startswith(("next", "pr")) and today.weekday() < 5:
            sat += dt.timedelta(days=7)
        found[m.group(0)] = f"{say(sat)} to {say(sat + dt.timedelta(days=1))}"
    if m := NEXT_WEEK.search(text):
        monday = today + dt.timedelta(days=7 - today.weekday())
        found[m.group(0)] = f"{say(monday)} to {say(monday + dt.timedelta(days=6))}"
    if not found:
        return ""
    parts = "; ".join(f"“{k}” = {v}" for k, v in found.items())
    return f"(Dates, worked out by the computer: {parts}. Use these dates, written as DD/MM/YYYY.)"
