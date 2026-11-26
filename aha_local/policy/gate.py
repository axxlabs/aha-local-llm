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
from .grants import Grant

Kind = Literal["read", "type", "open", "search", "send", "block", "other"]
Outcome = Literal["run", "card", "block"]


@dataclass(frozen=True)
class Action:
    kind: Kind
    site: str = ""  # the destination's registrable domain, from the browser's address or the URL
    url: str = ""  # for open (and a search engine's results page opened in the browser)
    reason: str = ""  # why it's blocked
    # A send's kind (email, draft, calendar…) and who receives it, when that can be allowed up front (grants);
    # None: nothing a grant covers (a memory, a background task, a deletion, a message typed on a site)
    what: str = ""
    to: tuple[str, ...] | None = None


@dataclass
class Facts:
    """What the harness itself knows about the task, never the model's words."""

    trusted: set[str] = field(default_factory=set)  # the person's trusted sites (§5.7)
    shown: set[tuple] = field(default_factory=set)  # addresses shown to the task (addresses.key)
    opened: set[str] = field(default_factory=set)  # sites opened in the task
    allow_all_sites: bool = True
    granted: Grant = field(default_factory=Grant)  # allowed up front by the person, for this topic (grants)


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    # run: "trusted" or "granted" (audited); card: which rule asked (WHY, shown on the card's (i)); block: the reason
    why: str = ""


def decide(a: Action, f: Facts) -> Verdict:
    """SPEC §5.2, rule by rule."""
    if a.kind == "block":
        return Verdict("block", a.reason or "this action is disabled")
    if a.kind == "send":
        return Verdict("run", "granted") if f.granted.covers(a.what, a.to) else Verdict("card", "send")
    # A search is its own rule, never a site's: trusting google.com is for typing there, not for every search's
    # words (SPEC §5.2 rule 6).
    if a.kind in ("type", "open") and a.site and a.site in f.trusted:
        return Verdict("run", "trusted")
    if a.kind == "read":
        return Verdict("run")
    if a.kind == "type":
        return Verdict("run", "granted") if f.granted.types(a.site) else Verdict("card", "type")
    if a.kind == "search":  # the words go to the search engine
        return Verdict("run", "granted") if f.granted.search else Verdict("card", "search")
    if a.kind == "open":
        # Opening its pages allowed up front covers the reading rules; an address that carries data hands it to
        # the site, a write, so only typing there allowed covers that.
        reads = f.granted.opens(a.site)
        new_site = not f.allow_all_sites and a.site not in f.opened
        if new_site and not reads:
            return Verdict("card", "new_site")
        if addresses.key(a.url) in f.shown:
            return Verdict("run")
        if addresses.data_pairs(a.url):
            return Verdict("run", "granted") if f.granted.types(a.site) else Verdict("card", "data")
        if a.site not in f.opened:
            return Verdict("run", "granted") if reads else Verdict("card", "open")
        return Verdict("run")
    return Verdict("card", "other")  # not classified yet


# Why a card is shown: the rule that asked, for the person (the (i) on the card, SPEC §7.4). Code decides cards
# (SPEC §5.2), so code says why. `{site}`: the action's site.
TRUST_HINT = " Add {site} to your trusted sites in Settings and it won't ask again."
WHY = {
    "send": "Sending a message to someone always asks first, on every site, trusted or not: nothing goes out in "
    "your name without your OK. You can change the text before it goes.",
    "type": "{site} isn't one of your trusted sites, so whatever Aha! types into it (a search, a form, an answer) "
    "is shown to you first." + TRUST_HINT,
    "search": "A web search sends its words to the search engine, so each search is shown to you first.",
    "open": "Opening a site the task hasn't opened yet could carry information out in the address, so it's shown "
    "to you first. Your trusted sites, links on pages it read, and sites already opened in this task don't ask."
    + TRUST_HINT,
    "data": "This address carries information after the site's name (in the part after '?': a name, an email, a "
    "code…), which {site} would receive. Check it before it opens; you can change it." + TRUST_HINT,
    "new_site": "Opening sites is set to ask first (Settings), so each new site in a task is shown to you before "
    "it opens.",
    "other": "Aha! doesn't know yet whether this action sends anything out, so it asks first.",
}


def explain(why: str, site: str = "") -> str:
    """The card's (i) text for a verdict's `why`; "" when there's none."""
    text = WHY.get(why, "")
    site = site or "this site"
    return text.replace("{site}", site).replace(" Add this site to", " Add the site to")
