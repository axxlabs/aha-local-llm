# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Dates and times, in one place (SPEC §3.5 "Dates", §3.4).

- The household's time zone (`tz`, `now`).
- What tools take, and cards show for editing: `YYYY-MM-DD HH:MM`, or `YYYY-MM-DD` for a whole day (`parse_local`,
  `when_of`, `editable`).
- What the model reads and writes: DD/MM/YYYY, never a weekday it worked out itself (`say`, `for_model`); the
  app shows each date with its weekday. A card may say the day in words (`in_words`), worked out here.

The days a message names are the model's to read, from the list of the coming days with their weekdays in the
context (2026-11-26: the regexes that worked them out of the person's words, in two languages, are gone).
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

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
