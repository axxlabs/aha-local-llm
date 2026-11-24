# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""web_search (SearXNG) and fetch_url (SPEC §3.4)."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import trafilatura

from .. import RELEASE
from ..approvals import Field
from ..policy.browser import open_action, open_form
from ..policy.gate import Action, explain
from ..policy.urls import check_url, resolves_publicly
from . import guard
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

MAX_RESULTS = 6
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
    action = open_action(url)
    verdict = guard.check(ctx, action, "fetch_url")
    if verdict.outcome == "block":
        return url, ToolResult(f"Blocked: {verdict.why}.", error=True)
    if verdict.outcome == "card":
        form = open_form(url, data=verdict.why.startswith("data"))
        why = explain(verdict.why, action.site)
        decision, refused = await guard.ask(ctx, "action", form.title, form.fields, why=why)
        if refused:
            return url, refused
        url = decision.values[0].strip()
        if why := check_url(url)[1]:
            return url, ToolResult(f"Blocked: {why}.", error=True)
    guard.opened(ctx, url)
    return url, None


async def web_search(args: dict, ctx: ToolContext) -> ToolResult:
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolResult("Give a search query.", error=True)
    verdict = guard.check(ctx, Action("search"), "web_search")
    if verdict.outcome == "card":  # SPEC §5.2 rule 6
        decision, refused = await guard.ask(
            ctx, "action", "Search the web for this?", [Field("Search", query)], why=explain(verdict.why)
        )
        if refused:
            return refused
        query = decision.values[0].strip()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{ctx.config.search_url}/search", params={"q": query, "format": "json"})
        r.raise_for_status()
        results = r.json().get("results", [])[:MAX_RESULTS]
    if not results:
        return ToolResult(f"No results for: {query}")
    lines = []
    for i, x in enumerate(results, 1):
        date = f" ({x['publishedDate'][:10]})" if x.get("publishedDate") else ""
        snippet = " ".join((x.get("content") or "").split())[:300]
        lines.append(f"{i}. {x.get('title', '').strip()}{date}\n   {x.get('url')}\n   {snippet}")
    return ToolResult(
        "Search results (snippets can be old or cut off; open a page before quoting numbers):\n" + "\n".join(lines)
    )


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
    from .browser import CANT_TELL, annotate, http_status, to_result

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
            return ToolResult(f"Blocked: {why}.", error=True)
        if not await resolves_publicly(urlsplit(url).hostname or ""):
            return ToolResult("Blocked: that name doesn't resolve to a public address.", error=True)
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
            "Search the web. Returns titles, links, dates and short snippets. Snippets are "
            "often old: open the page with fetch_url before relying on a number or a fact.",
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
