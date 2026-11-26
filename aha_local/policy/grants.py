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
GRANTABLE = ("email", "draft", "calendar", "calendar_change")  # the sends a grant can cover, by who receives them


def emails_in(text: str) -> list[str]:
    """Every address in a To, Cc or guests field, lower-cased."""
    return [m.lower() for m in EMAIL.findall(text or "")]


@dataclass
class Grant:
    """Each read kept apart from its write (2026-11-26: "one is read the other is write action"): opening a
    site's pages isn't typing on it, saving a draft isn't sending, adding an event isn't changing one."""

    open: set[str] = field(default_factory=set)  # open its pages (no data in the address), even holding private data
    type: set[str] = field(default_factory=set)  # type and fill in forms there, and addresses carrying data; opens too
    search: bool = False  # web searches even while the task holds private data
    email: set[str] = field(default_factory=set)  # who it may send email to (and invite)
    drafts: bool = False  # save drafts in their own mailbox, to anyone: nothing goes out
    calendar_add: bool = False  # add new events; guests only among `email`
    calendar_change: bool = False  # change events already there; guests only among `email`

    def __bool__(self) -> bool:
        return bool(
            self.open
            or self.type
            or self.search
            or self.email
            or self.drafts
            or self.calendar_add
            or self.calendar_change
        )

    def opens(self, site: str) -> bool:
        return bool(site) and (site in self.open or site in self.type)

    def types(self, site: str) -> bool:
        return bool(site) and site in self.type

    def covers(self, what: str, to: tuple[str, ...] | None) -> bool:
        """A send (SPEC §5.2 rule 2) of kind `what` to `to`: allowed up front? None: it has no recipients that
        could be allowed (a memory, a background task, a deletion, a message typed on a site)."""
        if to is None or what not in GRANTABLE:
            return False
        everyone = all(t in self.email for t in to)
        if what == "draft":
            return self.drafts or (bool(to) and everyone)
        if what == "calendar":
            return self.calendar_add and everyone
        if what == "calendar_change":
            return self.calendar_change and everyone
        return bool(to) and everyone

    def add(self, other: Grant) -> None:
        self.open |= other.open
        self.type |= other.type
        self.search = self.search or other.search
        self.email |= other.email
        self.drafts = self.drafts or other.drafts
        self.calendar_add = self.calendar_add or other.calendar_add
        self.calendar_change = self.calendar_change or other.calendar_change

    def labels(self) -> list[str]:
        """What it allows, in the person's words (the card, the Tasks panel)."""
        return (
            [type_label(s) for s in sorted(self.type)]
            + [open_label(s) for s in sorted(self.open - self.type)]
            + ([SEARCH_LABEL] if self.search else [])
            + [email_label(e) for e in sorted(self.email)]
            + ([DRAFTS_LABEL] if self.drafts else [])
            + ([CALENDAR_ADD_LABEL] if self.calendar_add else [])
            + ([CALENDAR_CHANGE_LABEL] if self.calendar_change else [])
        )

    def to_dict(self) -> dict:
        return {
            "open": sorted(self.open),
            "type": sorted(self.type),
            "search": self.search,
            "email": sorted(self.email),
            "drafts": self.drafts,
            "calendar_add": self.calendar_add,
            "calendar_change": self.calendar_change,
        }

    @classmethod
    def from_dict(cls, data: object) -> Grant:
        if not isinstance(data, dict):
            return cls()
        strings = lambda key: {s for s in data.get(key) or [] if isinstance(s, str) and s}  # noqa: E731
        yes = lambda key: data.get(key) is True  # noqa: E731
        # A task stored before the split (2026-11-26): its "sites" typed there, its "calendar" added and changed.
        return cls(
            strings("open"),
            strings("type") | strings("sites"),
            yes("search"),
            strings("email"),
            yes("drafts"),
            yes("calendar_add") or yes("calendar"),
            yes("calendar_change") or yes("calendar"),
        )


SEARCH_LABEL = "Search the web, even after reading your email or private pages"
DRAFTS_LABEL = "Save email drafts in your mailbox (nothing is sent)"
CALENDAR_ADD_LABEL = "Add new events to your calendar (guests only among the people above)"
CALENDAR_CHANGE_LABEL = "Change events already in your calendar (guests only among the people above)"


def open_label(site: str) -> str:
    return f"Open pages on {site} (read only), even after reading your email or private pages"


def type_label(site: str) -> str:
    return f"Type and fill in forms on {site} (searches, sign-ups, bookings)"


def email_label(address: str) -> str:
    return f"Send email to {address}"
