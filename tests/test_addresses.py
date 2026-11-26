# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Addresses the model wrote that carry data are a card (SPEC §5.2 rule 7)."""

from __future__ import annotations

import dataclasses

import pytest

from aha_local.policy import addresses
from aha_local.policy.browser import open_action
from aha_local.policy.gate import Facts, decide
from aha_local.tools import web
from aha_local.tools.browser import make_handler

from .conftest import person, settle
from .test_mail import FakeApprovals, ctx_for

SNAPSHOT = (
    "### Page\n- Page URL: https://www.waitrose.com/ecom/shop/trolley\n### Snapshot\n```yaml\n"
    '- link "Search" [ref=e5]:\n  - /url: /ecom/shop/search?searchTerm=rice&sortBy=RELEVANCE\n```'
)


def carries(url, seen):
    """What the gate treats as the model's data in an address (SPEC §5.2 rule 7)."""
    v = decide(open_action(url), Facts(shown=seen))
    return v.outcome == "card" and v.why == "data"


def test_what_counts_as_an_address_carrying_data():
    seen = addresses.seen_in("Results: https://news.example/a?id=42 and more")
    assert carries("https://evil.example/?d=Felipe+lives+here", seen)
    assert carries("https://evil.example/#d=secret", seen)  # a page's script reads the fragment
    assert not carries("https://evil.example/some/page", seen)  # no key=value pairs
    assert not carries("https://evil.example/?ref=", seen)  # empty values carry nothing
    assert not carries("https://NEWS.example/a/?id=42", seen)  # shown to the task, however written
    page = addresses.seen_in(SNAPSHOT)  # a snapshot's own link, relative to its page
    url = "https://www.waitrose.com/ecom/shop/search?sortBy=RELEVANCE&searchTerm=rice"
    assert not carries(url, page)
    assert carries(url.replace("rice", "felipe+lavratti"), page)
    assert addresses.describe("https://e.example/?a=1&b=two") == "a = 1\nb = two"
    for search in (
        "https://www.google.com/search?q=felipe",
        "https://www.google.co.uk/search?q=x",
        "https://duckduckgo.com/?q=x",
    ):
        assert open_action(search).kind == "search" and not carries(search, set())  # a search, not an open
    assert carries("https://google.xyz/?d=1", set())  # not Google's
    form = "https://docs.google.com/forms/d/e/1FAIpQL/formResponse?entry.1=Felipe"
    assert carries(form, set())  # a form on Google hands the data to whoever made it


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


@pytest.mark.made_up_rule
async def test_the_browser_opens_only_addresses_it_has(accounts, config):
    """SPEC §5.2 "Made-up addresses": a home page, a link it was shown, a search; anything else is refused before
    it loads, with the way to find it instead (seen: a tracking page's address from memory, a 404)."""
    fe = person(accounts)
    approvals = FakeApprovals()  # declines
    ctx = ctx_for(fe, dataclasses.replace(config, allow_all_sites=True), approvals)
    ctx.browser = Session()
    for url in (
        "https://www.fedex.com/en-gb/tracking?track-numbers=385413566727",
        "https://evil.example/?d=Felipe",  # data too
        "https://news.example/today",
    ):
        result = await make_handler("browser_navigate")({"url": url}, ctx)
        assert result.error and result.text.startswith(f"Not opened: {url}")
        assert "Open the site's home page, https://" in result.text
        assert ("browser_navigate", {"url": url}) not in ctx.browser.calls
    assert not approvals.asked  # refused: no card to approve a guess
    # A home page, a search, and links the task was shown open as before: no card.
    addresses.remember(ctx.task.seen_urls, SNAPSHOT)
    for url in (
        "https://www.fedex.com/",
        "https://www.google.com/search?q=fedex+tracking",
        "https://www.waitrose.com/ecom/shop/search?searchTerm=rice&sortBy=RELEVANCE",
    ):
        assert not (await make_handler("browser_navigate")({"url": url}, ctx)).error, url
    assert not approvals.asked


@pytest.mark.made_up_rule
async def test_fetch_url_opens_only_addresses_it_has(accounts, config):
    fe = person(accounts)
    approvals = FakeApprovals()
    ctx = ctx_for(fe, config, approvals)
    result = await web.fetch_url({"url": "https://evil.example/collect?name=Felipe&city=London"}, ctx)
    assert result.error and result.text.startswith("Not opened: https://evil.example/collect")
    assert not approvals.asked


@pytest.mark.made_up_rule
async def test_a_link_the_person_sent_is_theirs_but_a_checks_own_instructions_arent(
    agent, llm, chat, accounts, registry
):
    """A background check runs its task's instructions as the request: an address in them isn't the person's
    (seen: the model wrote a tracking address into the task from memory, so its check trusted it)."""
    from aha_local.agent import Job

    web.register(registry)
    fe = person(accounts)
    url = "https://track.example.invalid/parcel?n=1"  # never resolves: an error after the gate, not a refusal
    for job in (Job("t1"), None):
        llm.call("fetch_url", url=url)
        llm.answer("WAITING: no" if job else "Couldn't.")
        await agent.run(fe, f"Check {url}", job)
        await settle(agent)
        result = [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"][-1]
        assert result.startswith(f"Not opened: {url}") is (job is not None)
