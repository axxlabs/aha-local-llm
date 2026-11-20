# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Which URLs the browser and fetch_url may open (SPEC §5.2). Decided from the URL itself, in code."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

TWO_LEVEL_SUFFIXES = {"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go"}
LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".arpa")


def site_of(host: str) -> str:
    """Approximate registrable domain: www.restaurant.co.uk → restaurant.co.uk."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def check_url(url: str) -> tuple[str | None, str | None]:
    """(site, None) for a public http(s) URL; (None, reason) when it must be blocked."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None, "not a valid web address"
    if parts.scheme not in ("http", "https"):
        return None, f"only http and https pages can be opened (got '{parts.scheme or 'none'}')"
    if not host:
        return None, "the address has no host"
    if parts.username or parts.password:
        return None, "addresses with embedded passwords are not allowed"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        return (host, None) if ip.is_global else (None, "local and private network addresses are blocked")
    if "." not in host or host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return None, "local network names are blocked"
    return site_of(host), None


async def resolves_publicly(host: str) -> bool:
    """Every address the name resolves to must be public (defence against DNS pointing at the LAN)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)
