# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""web_search (Google's results page, in the browser) and fetch_url (SPEC §3.4)."""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit

import httpx
import trafilatura

from .. import RELEASE
from ..approvals import Field
from ..policy.gate import Action, explain
from ..policy.urls import check_url, resolves_publicly
from . import guard
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

MAX_PAGE_BYTES = 5_000_000
MAX_PAGE_CHARS = 12_000
MAX_REDIRECTS = 5
# Say what we are (SPEC §3.4): a client that claims to be Chrome without Chrome's TLS handshake is what bot
# protection looks for, and gets refused where an honest one is let in.
USER_AGENT = f"Aha/{RELEASE} (+https://github.com/axxlabs/aha-local-llm)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


async def gated_open(ctx: ToolContext, url: str) -> tuple[str, ToolResult | None]:
    """fetch_url's address through the gate (SPEC §5.2): the address to read, or the result to return."""
    url, _, refused = await guard.open_address(ctx, url, "fetch_url")
    if refused is None:
        guard.opened(ctx, url)
    return url, refused


async def web_search(args: dict, ctx: ToolContext) -> ToolResult:
    """Google's own results page, read in the person's browser (SPEC §3.4 "Web search"): its answer box first
    (directions, weather, opening hours, prices…), then the results. A list of links from a search proxy never
    had those (seen: a drive time took three cards and a typed search on google.com)."""
    query = " ".join(str(args.get("query", "")).split())
    if not query:
        return ToolResult("Give a search query.", error=True)
    if ctx.browser is None:
        return ToolResult("Searching needs the browser, which isn't available right now.", error=True)
    # Its own rule (SPEC §5.2 rule 6): one "Search Google for this?" card with the words; never google.com's site
    # cards, and trusting google.com doesn't skip it.
    verdict = guard.check(ctx, Action("search"), "web_search")
    if verdict.outcome == "block":
        return guard.blocked(verdict.why)
    if verdict.outcome == "card":  # SPEC §5.2 rule 6: the words that would go out, shown first
        decision, refused = await guard.ask(
            ctx, "action", "Search Google for this?", [Field("Search", query)], why=explain(verdict.why)
        )
        if refused:
            return refused
        query = " ".join(decision.values[0].split()) or query
    from .browser_page import call_with_snapshot

    url = google_url(query)
    result = await call_with_snapshot(ctx.browser, "browser_navigate", {"url": url}, ctx)
    if not result.error:
        guard.opened(ctx, url)
    result.text = f"Google's results for “{query}” (its answer box, when it has one, comes first):\n{result.text}"
    return result


def google_url(query: str) -> str:
    return "https://www.google.com/search?" + urlencode({"q": query})


class Fetched:
    def __init__(self, url: str, status: int, ctype: str, body: bytes, redirect: str | None) -> None:
        self.url, self.status, self.ctype, self.body, self.redirect = url, status, ctype, body, redirect


async def get_once(url: str) -> Fetched:
    """One request, no redirects followed, the body capped at MAX_PAGE_BYTES."""
    async with (
        httpx.AsyncClient(timeout=20, follow_redirects=False, headers=HEADERS) as c,
        c.stream("GET", url) as r,
    ):
        if r.is_redirect and "location" in r.headers:
            return Fetched(str(r.url), r.status_code, "", b"", str(r.url.join(r.headers["location"])))
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > MAX_PAGE_BYTES:
                break
        return Fetched(str(r.url), r.status_code, r.headers.get("content-type", ""), bytes(body), None)


BLOCKED_STATUSES = {401, 403, 405, 406, 408, 429, 451, 503}


async def read_with_browser(url: str, ctx: ToolContext, why: str) -> ToolResult:
    """Many sites block plain HTTP clients but not a real browser. The URL was already checked."""
    if ctx.browser is None:
        return ToolResult(f"Couldn't read {url}: {why}. Try the browser tools.", error=True)
    from .browser_page import CANT_TELL, annotate, http_status, to_result
    from .page_view import for_model

    result = to_result(await ctx.browser.call("browser_navigate", {"url": url}))
    annotate(ctx, result)  # a sign-in form or a human check on it is said here too (SPEC §3.4)
    status = http_status(result.text)
    if ctx.task.challenge_page:  # an "are you human?" check: the person can do it on a card (SPEC §3.4)
        ctx.task.opened_sites.add(check_url(url)[0] or "")
        return result
    if result.error or (status and status >= 400):
        also = f"answered HTTP {status} to the browser too" if status else "and the browser failed too"
        return ToolResult(f"Couldn't read {url}: {why}, {also}. {CANT_TELL}", error=True)
    ctx.task.opened_sites.add(check_url(url)[0] or "")
    for_model(ctx, result)
    result.text = f"(Read with the browser because {why}.)\n{result.text}"
    return result


async def fetch_url(args: dict, ctx: ToolContext) -> ToolResult:
    url = str(args.get("url", "")).strip()
    url, refused = await gated_open(ctx, url)
    if refused:
        return refused
    page = None
    for _ in range(MAX_REDIRECTS + 1):
        _, why = check_url(url)
        if why:
            return guard.blocked(why)
        if not await resolves_publicly(urlsplit(url).hostname or ""):
            return guard.blocked("that name doesn't resolve to a public address")
        page = await get_once(url)
        if page.redirect is None:
            break
        url = page.redirect  # checked again at the top of the loop
    if page is None or page.redirect is not None:
        return ToolResult("Too many redirects.", error=True)
    if page.status in BLOCKED_STATUSES:
        return await read_with_browser(url, ctx, f"the site refused a simple request (HTTP {page.status})")
    if page.status >= 400:
        return ToolResult(f"The page returned HTTP {page.status}.", error=True)
    final_url, ctype, body = page.url, page.ctype, page.body
    html = body.decode("utf-8", errors="replace")
    if "html" not in ctype and "xml" not in ctype:
        return ToolResult(f"{final_url} ({ctype or 'unknown type'}):\n{html[:MAX_PAGE_CHARS]}", kind="page")
    meta = trafilatura.extract_metadata(html)
    text = trafilatura.extract(html, url=final_url, include_tables=True, include_links=False, favor_recall=True)
    if not text:
        return await read_with_browser(final_url, ctx, "the page has no readable text without JavaScript")
    title = (meta.title if meta and meta.title else "") or final_url
    date = f"\nPage date: {meta.date}" if meta and meta.date else ""
    cut = "\n[… page cut to fit]" if len(text) > MAX_PAGE_CHARS else ""
    return ToolResult(f"{title}\n{final_url}{date}\n\n{text[:MAX_PAGE_CHARS]}{cut}", kind="page")


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "web_search",
            "Search the web with Google. Returns Google's results page: its answer box first when it has one "
            "(directions and travel times, weather, opening hours, prices, flights, scores, conversions), then "
            "the results with their links. Click a result or open it with fetch_url for the details.",
            params(["query"], query=string("Specific search words, e.g. 'Flat Iron Kings Cross opening hours'")),
            web_search,
        )
    )
    reg.add(
        Tool(
            "fetch_url",
            "Read a web page: returns its main text, title and date. Use it to check facts and "
            "numbers on the actual page.",
            params(["url"], url=string("The page's full http(s) address")),
            fetch_url,
        )
    )
