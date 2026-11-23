# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The gate (SPEC §5.2): the one place that decides whether an action runs, shows a card, or is blocked.

Every tool call that can send something out is described as an `Action` (what kind, where it goes, what it
carries) and passed to `decide()` with what the harness knows about the task (`Facts`). Nothing else decides a
policy card. The model never sees the outcome as such: a tool runs or it doesn't (SPEC §5.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from . import addresses

Kind = Literal["read", "type", "open", "search", "send", "block", "other"]
Outcome = Literal["run", "card", "block"]


@dataclass(frozen=True)
class Action:
    kind: Kind
    site: str = ""  # the destination's registrable domain, from the browser's address or the URL
    url: str = ""  # for open (and a search engine's results page opened in the browser)
    reason: str = ""  # why it's blocked


@dataclass
class Facts:
    """What the harness itself knows about the task, never the model's words."""

    trusted: set[str] = field(default_factory=set)  # the person's trusted sites (§5.7)
    private: bool = False  # the task read mail or had a page of a signed-in site (§5.2 "Private data")
    shown: set[tuple] = field(default_factory=set)  # addresses shown to the task (addresses.key)
    opened: set[str] = field(default_factory=set)  # sites opened in the task
    allow_all_sites: bool = True


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    why: str = ""  # "trusted" (audited), "data" (the address carries data), or the block reason


def decide(a: Action, f: Facts) -> Verdict:
    """SPEC §5.2, rule by rule."""
    if a.kind == "block":
        return Verdict("block", a.reason or "this action is disabled")
    if a.kind == "send":
        return Verdict("card")
    if a.kind in ("type", "open", "search") and a.site and a.site in f.trusted:
        return Verdict("run", "trusted")
    if a.kind == "read":
        return Verdict("run")
    if a.kind == "type":
        return Verdict("card")
    if a.kind == "search":
        return Verdict("card") if f.private else Verdict("run")
    if a.kind == "open":
        if not f.allow_all_sites and a.site not in f.opened:
            return Verdict("card")
        if addresses.key(a.url) in f.shown:
            return Verdict("run")
        if addresses.data_pairs(a.url):
            return Verdict("card", "data")
        if f.private and a.site not in f.opened:
            return Verdict("card")
        return Verdict("run")
    return Verdict("card")  # not classified yet
