# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Permissions allowed up front (SPEC §5.2 "Allowed up front").

At the start of a request the model says what it expects to need (`permissions_request`); the person allows it
on one card, and for the rest of the topic those actions run without their own card, like a trusted site. A
background task started then keeps them for its checks. Only what the person ticked is in a `Grant`: the model's
words never reach it any other way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[a-z]{2,63}", re.I)
GRANTABLE = ("email", "draft", "calendar")  # the sends a grant can cover, by who receives them


def emails_in(text: str) -> list[str]:
    """Every address in a To, Cc or guests field, lower-cased."""
    return [m.lower() for m in EMAIL.findall(text or "")]


@dataclass
class Grant:
    sites: set[str] = field(default_factory=set)  # type and open any address there, even holding private data
    search: bool = False  # web searches even while the task holds private data
    email: set[str] = field(default_factory=set)  # who it may email (send or save a draft to), and invite
    calendar: bool = False  # add or change calendar events; guests only among `email`

    def __bool__(self) -> bool:
        return bool(self.sites or self.search or self.email or self.calendar)

    def covers(self, what: str, to: tuple[str, ...] | None) -> bool:
        """A send (SPEC §5.2 rule 2) of kind `what` to `to`: allowed up front? None: it has no recipients that
        could be allowed (a memory, a background task, a deletion, a message typed on a site)."""
        if to is None or what not in GRANTABLE:
            return False
        if what == "calendar":
            return self.calendar and all(t in self.email for t in to)
        return bool(to) and all(t in self.email for t in to)

    def add(self, other: Grant) -> None:
        self.sites |= other.sites
        self.search = self.search or other.search
        self.email |= other.email
        self.calendar = self.calendar or other.calendar

    def labels(self) -> list[str]:
        """What it allows, in the person's words (the card, the Tasks panel)."""
        return (
            [site_label(s) for s in sorted(self.sites)]
            + ([SEARCH_LABEL] if self.search else [])
            + [email_label(e) for e in sorted(self.email)]
            + ([CALENDAR_LABEL] if self.calendar else [])
        )

    def to_dict(self) -> dict:
        return {
            "sites": sorted(self.sites),
            "search": self.search,
            "email": sorted(self.email),
            "calendar": self.calendar,
        }

    @classmethod
    def from_dict(cls, data: object) -> Grant:
        if not isinstance(data, dict):
            return cls()
        strings = lambda key: {s for s in data.get(key) or [] if isinstance(s, str) and s}  # noqa: E731
        return cls(strings("sites"), data.get("search") is True, strings("email"), data.get("calendar") is True)


SEARCH_LABEL = "Search the web, even after reading your email or private pages"
CALENDAR_LABEL = "Add or change events in your calendar (guests only among the people above)"


def site_label(site: str) -> str:
    return f"Open pages and type on {site}"


def email_label(address: str) -> str:
    return f"Email {address} (send, or save a draft)"
