# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""fetch_url's address checks (SPEC §5.2) and the rendered Quadlet units (SPEC §3.6)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from aha_local.tools import web


@pytest.fixture
def ctx(config):
    return SimpleNamespace(config=config)


async def test_fetch_blocks_private_addresses(ctx):
    for url in ("http://127.0.0.1:8081/v1/models", "http://192.168.1.1", "file:///etc/passwd"):
        r = await web.fetch_url({"url": url}, ctx)
        assert r.error and r.text.startswith("Blocked")


async def test_fetch_blocks_a_redirect_to_the_lan(ctx, monkeypatch):
    async def public(host):
        return True

    async def once(url):
        return web.Fetched(url, 302, "", b"", "http://10.0.0.1/admin")

    monkeypatch.setattr(web, "resolves_publicly", public)
    monkeypatch.setattr(web, "get_once", once)
    r = await web.fetch_url({"url": "https://example.com/go"}, ctx)
    assert r.error and "private" in r.text


async def test_fetch_blocks_names_that_resolve_to_the_lan(ctx, monkeypatch):
    async def private(host):
        return False

    monkeypatch.setattr(web, "resolves_publicly", private)
    r = await web.fetch_url({"url": "https://rebind.example.com/"}, ctx)
    assert r.error and "public address" in r.text


async def test_fetch_extracts_the_main_text(ctx, monkeypatch):
    html = (
        b"<html><head><title>Flat Iron Kings Cross</title></head><body><nav>menu</nav><article><h1>Opening "
        b"hours</h1><p>Monday to Sunday, 12:00 to 22:00. Walk-ins welcome, bookings for groups.</p>"
        b"<p>9 Pancras Square, London N1C 4AG.</p></article></body></html>"
    )

    async def public(host):
        return True

    async def once(url):
        return web.Fetched(url, 200, "text/html", html, None)

    monkeypatch.setattr(web, "resolves_publicly", public)
    monkeypatch.setattr(web, "get_once", once)
    r = await web.fetch_url({"url": "https://flatironsteak.co.uk/kings-cross"}, ctx)
    assert not r.error and "12:00 to 22:00" in r.text and "https://flatironsteak.co.uk/kings-cross" in r.text
    assert "menu" not in r.text  # navigation is dropped


class FakeBrowser:
    def __init__(self, status: int | None) -> None:
        self.status = status
        self.calls: list[tuple] = []

    async def call(self, name, args, timeout=90):
        import mcp.types as mt

        self.calls.append((name, args))
        line = f"- HTTP status: {self.status}\n" if self.status else ""
        text = f"### Page\n- Page URL: {args['url']}\n{line}- Page Title: Laptop 13\n### Snapshot\nLaptop 13 Pro"
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)], isError=False)


def browser_ctx(ctx, status):
    return SimpleNamespace(config=ctx.config, browser=FakeBrowser(status), task=SimpleNamespace(approved_sites=set()))


async def test_fetch_says_what_it_is():
    assert web.HEADERS["User-Agent"].startswith("Aha/") and "Chrome" not in web.HEADERS["User-Agent"]
    assert "text/html" in web.HEADERS["Accept"] and web.HEADERS["Accept-Language"]


@pytest.mark.parametrize("status", [408, 403, 429])
async def test_refused_or_timed_out_pages_are_read_with_the_browser(ctx, monkeypatch, status):
    async def public(host):
        return True

    async def once(url):
        return web.Fetched(url, status, "text/html", b"", None)

    monkeypatch.setattr(web, "resolves_publicly", public)
    monkeypatch.setattr(web, "get_once", once)
    bctx = browser_ctx(ctx, None)
    r = await web.fetch_url({"url": "https://frame.work/gb/en/laptop13pro"}, bctx)
    assert not r.error and "Laptop 13 Pro" in r.text and f"HTTP {status}" in r.text
    assert bctx.browser.calls[0][0] == "browser_navigate"


async def test_a_browser_http_error_is_reported_plainly(ctx, monkeypatch):
    async def public(host):
        return True

    async def once(url):
        return web.Fetched(url, 408, "text/html", b"", None)

    monkeypatch.setattr(web, "resolves_publicly", public)
    monkeypatch.setattr(web, "get_once", once)
    r = await web.fetch_url({"url": "https://frame.work/x"}, browser_ctx(ctx, 408))
    assert r.error and "HTTP 408 to the browser too" in r.text and "don't guess" in r.text


def test_browser_results_flag_http_errors():
    import mcp.types as mt

    from aha_local.tools.browser import to_result

    page = "### Page\n- Page URL: https://x.example\n- HTTP status: 503\n### Snapshot\n…"
    r = to_result(mt.CallToolResult(content=[mt.TextContent(type="text", text=page)], isError=False))
    assert r.text.startswith("The site answered HTTP 503.") and "don't guess" in r.text
    ok = to_result(mt.CallToolResult(content=[mt.TextContent(type="text", text="- HTTP status: 200")], isError=False))
    assert not ok.text.startswith("The site")


def test_the_browser_image_is_pinned_with_a_normal_user_agent():
    unit = (Path(__file__).resolve().parents[1] / "deploy/quadlet/aha-browser.container").read_text()
    assert "Image=mcr.microsoft.com/playwright/mcp@sha256:" in unit
    assert '--user-agent "Mozilla/5.0' in unit and "HeadlessChrome" not in unit.split("Exec=")[1]


def test_units_render_with_sandboxing(tmp_path, monkeypatch):
    from aha_local import cli
    from aha_local.config import Config

    monkeypatch.setattr(cli, "HOME_DATA", tmp_path / "data")
    config = Config(
        gateway_url="unix:/run/user/1000/aha-gateway/gateway.sock",
        gateway_pin="x",
        models_dir=tmp_path / "models",
        documents=tmp_path / "docs",
    )
    units = cli.render_units(config)
    llm = units["aha-llm.container"]
    assert "Network=aha-internal.network" in llm and "aha-egress" not in llm
    assert "DropCapability=ALL" in llm and "ReadOnly=true" in llm
    agent = units["aha-agent.container"]
    assert "Network=aha-internal.network" in agent and "Network=aha-egress.network" in agent
    assert f"Volume={tmp_path / 'docs'}:/documents:ro" in agent
    assert "Volume=/run/user/1000/aha-gateway:/run/user/1000/aha-gateway" in agent
    assert "Internal=true" in units["aha-internal.network"]
    for text in units.values():
        assert "@DATA@" not in text and "@SEARCH_SECRET@" not in text


def test_every_source_file_carries_the_licence_header():
    root = Path(__file__).resolve().parents[1]
    missing = [
        str(f)
        for d in ("aha_local", "tests")
        for f in (root / d).rglob("*.py")
        if "SPDX-License-Identifier: AGPL-3.0-or-later" not in f.read_text()
    ]
    assert missing == []


def test_every_image_is_pinned_by_digest():
    """No moving tags (SPEC §9.2): an upstream tag change can't slip into an install."""
    import re

    deploy = Path(__file__).resolve().parents[1] / "deploy"
    refs = []
    for f in [*(deploy / "quadlet").glob("*.container"), deploy / "Containerfile"]:
        refs += re.findall(r"^(?:Image=|FROM |COPY --from=)(\S+)", f.read_text(), re.M)
    refs = [r for r in refs if r != "@AGENT_IMAGE@"]
    assert len(refs) >= 5
    for r in refs:
        assert re.search(r"@sha256:[0-9a-f]{64}$", r), r
