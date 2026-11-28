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
from .chats import is_chat_site

Kind = Literal["read", "type", "open", "search", "send", "block", "other"]
Outcome = Literal["run", "card", "block", "refuse"]


@dataclass(frozen=True)
class Action:
    kind: Kind
    site: str = ""  # the destination's registrable domain, from the browser's address or the URL
    url: str = ""  # for open
    reason: str = ""  # why it's blocked
    # A send's kind (email, draft, calendar…) and who receives it (an "always" tick is for that kind and those people);
    # None: no one in particular (a memory, a topic deferred, a deletion, a message typed on a site)
    what: str = ""
    to: tuple[str, ...] | None = None
    # A message typed on a site: the chat it goes to, read from the page by the harness (policy/chats.py), never
    # from the model's words; shown on its card
    chat: str = ""


@dataclass
class Facts:
    """What the harness itself knows about the task, never the model's words."""

    trusted: set[str] = field(default_factory=set)  # the person's trusted sites (§5.7)
    shown: set[tuple] = field(default_factory=set)  # addresses shown to the task (addresses.key)
    opened: set[str] = field(default_factory=set)  # sites opened in the task


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    # run: "trusted" (audited); card: which rule asked (WHY, shown on the card's (i)); block: the reason;
    # refuse: an address it may not open, said to the model ("made_up", "search")
    why: str = ""


def decide(a: Action, f: Facts) -> Verdict:
    """SPEC §5.2, rule by rule."""
    if a.kind == "block":
        return Verdict("block", a.reason or "this action is disabled")
    if a.kind == "send" and a.site:  # a message typed on a site: a chat, user-driven (policy/chats.py)
        return Verdict("card", "message")
    if a.kind == "send":
        return Verdict("card", "send")
    if a.kind == "type" and is_chat_site(a.site):  # anything typed on a chat site, trusted or not
        return Verdict("card", "chat")
    if a.kind == "search":  # web_search: the words go to the search engine, whatever site is trusted
        return Verdict("card", "search")
    if a.kind == "open":
        return opening(a, f)
    if a.kind == "type" and a.site in f.trusted and a.site:
        return Verdict("run", "trusted")
    if a.kind == "read":
        return Verdict("run")
    if a.kind == "type":
        return Verdict("card", "type")
    return Verdict("card", "other")  # not classified yet


def opening(a: Action, f: Facts) -> Verdict:
    """Opening an address (SPEC §5.2 rule 7), one rule: a search engine's results page is a search, done with
    web_search; an address the task was shown opens; one the model put together is refused; a site's home page
    opens on a trusted site or one already opened in the task, and otherwise asks once."""
    if addresses.is_search(a.url):
        return Verdict("refuse", "search")
    if addresses.key(a.url) in f.shown:
        return Verdict("run")
    if addresses.made_up(a.url, f.shown):
        return Verdict("refuse", "made_up")
    if a.site and a.site in f.trusted:
        return Verdict("run", "trusted")
    if a.site in f.opened:
        return Verdict("run")
    return Verdict("card", "open")


# Why a card is shown: the rule that asked, for the person (the (i) on the card, SPEC §7.4). Code decides cards
# (SPEC §5.2), so code says why. `{site}`: the action's site.
TRUST_HINT = " Add {site} to your trusted sites in Settings and it won't ask again."
WHY = {
    "send": "Sending a message to someone always asks first, on every site, trusted or not: nothing goes out in "
    "your name without your OK. You can change the text before it goes.",
    "message": "A message typed into a chat always asks first, on every site, trusted or not: nothing goes out in "
    'your name without your OK. The card names the chat it goes to, as the page shows it; "Always allow" covers '
    "only that chat. You can change the text before it goes.",
    "chat": "Everything typed on {site} is shown to you first, every time, even a search: on a chat site, what "
    "sha types is yours to decide.",
    "type": "{site} isn't one of your trusted sites, so whatever sha types into it (a search, a form, an answer) "
    "is shown to you first." + TRUST_HINT,
    "search": "A web search sends its words to the search engine, so each search is shown to you first.",
    "open": "sha wants to open a site it hasn't opened in this task, so it's shown to you first. Links on pages it "
    "read, and sites already opened in this task, don't ask." + TRUST_HINT,
    "other": "sha doesn't know yet whether this action sends anything out, so it asks first.",
}


def explain(why: str, site: str = "") -> str:
    """The card's (i) text for a verdict's `why`; "" when there's none."""
    text = WHY.get(why, "")
    site = site or "this site"
    return text.replace("{site}", site).replace(" Add this site to", " Add the site to")
