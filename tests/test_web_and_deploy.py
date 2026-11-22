# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""fetch_url's address checks (SPEC §5.2) and the rendered Quadlet units (SPEC §3.6)."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from aha_local.tools import web
from aha_local.tools.registry import TaskState


@pytest.fixture
def ctx(config, accounts):
    return SimpleNamespace(config=config, task=TaskState(), account=accounts.create("Test Person"))


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
    return SimpleNamespace(config=ctx.config, browser=FakeBrowser(status), task=TaskState(), account=ctx.account)


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


def test_the_browser_looks_like_a_persons(tmp_path, monkeypatch):
    """SPEC §3.4 "Looking like a person's browser", test 20: headed, its own User-Agent, the household's ways."""
    from aha_local import cli
    from aha_local.config import Config

    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/quadlet/aha-browser.container").read_text()
    assert "Image=mcr.microsoft.com/playwright/mcp@sha256:" in unit
    entry = json.loads(re.search(r"^Entrypoint=(.+)$", unit, re.M).group(1))
    assert entry[:4] == ["/usr/bin/tini", "-s", "--", "xvfb-run"] and "--headless" not in entry
    exec_line = re.search(r"^Exec=(.+)$", unit, re.M).group(1)
    for flag in ("--headless", "--user-agent", "--viewport-size", "--shared-browser-context"):
        assert flag not in exec_line, flag
    assert "--isolated" in exec_line and "--config /etc/aha-browser/config.json" in exec_line

    monkeypatch.setattr(cli, "HOME_DATA", tmp_path / "data")
    config = Config(
        gateway_url="wss://g.example/v1/tunnel", gateway_pin="x", timezone="Europe/Lisbon", language="pt-PT"
    )
    browser_config = json.loads(cli.render_browser_config(config))["browser"]
    launch, context = browser_config["launchOptions"], browser_config["contextOptions"]
    assert launch["headless"] is False and launch["ignoreDefaultArgs"] == ["--enable-automation"]
    assert "--disable-blink-features=AutomationControlled" in launch["args"] and "--lang=pt-PT" in launch["args"]
    assert context == {"locale": "pt-PT", "timezoneId": "Europe/Lisbon", "viewport": None}
    assert browser_config["initScript"] == ["/etc/aha-browser/init.js"]
    rendered = cli.render_units(config)["aha-browser.container"]
    assert "Environment=TZ=Europe/Lisbon" in rendered and "Environment=LANG=pt_PT.UTF-8" in rendered
    assert f"Volume={cli.BROWSER_CONFIG}:/etc/aha-browser/config.json:ro" in rendered
    init = (root / "deploy/browser/init.js").read_text()
    assert "SPDX-License-Identifier: AGPL-3.0-or-later" in init and "new Proxy" in init


def test_the_browser_language_comes_from_the_computer(monkeypatch):
    from aha_local.config import host_language, language_tag

    assert language_tag("en_GB.UTF-8") == "en-GB" and language_tag("pt-br") == "pt-BR" and language_tag("de") == "de"
    assert language_tag("nonsense; rm -rf") == "en-GB"
    monkeypatch.setenv("LC_ALL", "")
    monkeypatch.setenv("LC_MESSAGES", "")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert host_language() == "fr-FR"
    monkeypatch.setenv("LANG", "C.UTF-8")
    assert host_language() == "en-GB"


def test_units_render_with_sandboxing(tmp_path, monkeypatch):
    from aha_local import cli
    from aha_local.config import Config

    monkeypatch.setattr(cli, "HOME_DATA", tmp_path / "data")
    config = Config(
        gateway_url="unix:/run/user/1000/aha-gateway/gateway.sock",
        gateway_pin="x",
        models_dir=tmp_path / "models",
    )
    units = cli.render_units(config)
    llm = units["aha-llm.container"]
    assert "Network=aha-internal.network" in llm and "aha-egress" not in llm
    assert "DropCapability=ALL" in llm and "ReadOnly=true" in llm
    agent = units["aha-agent.container"]
    assert "Network=aha-internal.network" in agent and "Network=aha-egress.network" in agent
    assert "/documents" not in agent
    assert "Volume=/run/user/1000/aha-gateway:/run/user/1000/aha-gateway" in agent
    assert "Internal=true" in units["aha-internal.network"]
    # The data key: unsealed by systemd into the unit's credentials folder, mounted read-only (SPEC §3.8.1).
    assert f"LoadCredentialEncrypted=data-key:{cli.DATA_KEY_CRED}" in agent
    assert "Volume=%d/data-key:/run/aha/data-key:ro" in agent
    assert "AHA_DATA_KEY" not in agent and "Environment" not in agent
    for text in units.values():
        assert not re.search(r"@[A-Z_]+@", text)


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
