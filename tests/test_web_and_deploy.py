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
