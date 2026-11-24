# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Addresses for the gate (SPEC §5.2 rule 7): which were shown to the task, and what one carries.

Opening an address sends it: `https://x.example/?d=<anything>` hands the text to that site without any typing.
An address shown to the task (a link on a page, a search result, a link the person sent) is the page's or the
person's; one the model wrote with `key=value` pairs in it carries the model's data. A search engine's own
results page is a search, not an open.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urljoin, urlsplit

from .urls import site_of

URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'`)\]|]+", re.I)
PAGE_URL = re.compile(r"^- Page URL: (\S+)", re.M)
SNAPSHOT_LINK = re.compile(r'/url: "?([^"\s]+)"?')
MAX_SEEN = 20_000  # addresses remembered per task
GOOGLE = re.compile(r"^google\.(?:com|co\.[a-z]{2}|com\.[a-z]{2}|[a-z]{2})$")  # Google's own, not google.xyz
SEARCH_SITES = {"bing.com", "duckduckgo.com", "brave.com", "startpage.com", "ecosia.org", "qwant.com", "yahoo.com"}


SEARCH_PATHS = {"/", "/search", "/html", "/sp/search", "/do/search"}


def is_search(url: str) -> bool:
    """A search engine's own results page: opening it is a search, not an open (SPEC §5.2)."""
    k = key(url)
    return k is not None and search_engine(k[0], k[1])


def search_engine(host: str, path: str) -> bool:
    """A search engine's own results page (www.google.com/search, duckduckgo.com/?q=…), nothing else of the site:
    docs.google.com/forms/…/formResponse?entry.1=… would hand the data to whoever made the form."""
    host = host.lower()
    site = site_of(host)
    if not (site in SEARCH_SITES or GOOGLE.match(site)):
        return False
    return host in (site, f"www.{site}", f"search.{site}") and (path.rstrip("/") or "/") in SEARCH_PATHS


def key(url: str) -> tuple | None:
    """An address in a form that compares equal however it's written (host case, a trailing slash, order)."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    return (
        p.hostname.lower(),
        p.path.rstrip("/") or "/",
        tuple(sorted(parse_qsl(p.query, keep_blank_values=True))),
        tuple(sorted(parse_qsl(p.fragment, keep_blank_values=True))),
    )


def seen_in(text: str) -> set[tuple]:
    """The addresses a tool result or message shows: plain ones, and a snapshot's links resolved on its page."""
    found = {k for u in URL_IN_TEXT.findall(text) if (k := key(u.rstrip(".,;:")))}
    page = PAGE_URL.search(text)
    if page:
        for link in SNAPSHOT_LINK.findall(text):
            if k := key(urljoin(page.group(1), link)):
                found.add(k)
    return found


def remember(seen: set[tuple], text: str) -> None:
    if len(seen) < MAX_SEEN:
        seen |= seen_in(text)


def made_up(url: str, seen: set[tuple]) -> bool:
    """An address past a site's home page that nothing in the task showed (a page, an email, a search result, the
    person's message): the model put it together itself. Seen: a guessed booking-management address."""
    k = key(url)
    return k is not None and k not in seen and (k[1] != "/" or bool(k[2])) and not is_search(url)


def data_pairs(url: str) -> list[tuple[str, str]]:
    """The `key=value` pairs an address carries, in its query and its fragment (a page's script reads both)."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return []
    return [(k, v) for k, v in parse_qsl(p.query) + parse_qsl(p.fragment) if v]


def describe(url: str, limit: int = 300) -> str:
    text = "\n".join(f"{k} = {v}" for k, v in data_pairs(url))
    return text if len(text) <= limit else text[: limit - 1] + "…"
