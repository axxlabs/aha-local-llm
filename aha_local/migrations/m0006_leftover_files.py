# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Files nothing reads any more: each account's `schedule.json` (an earlier scheduler), the SearXNG secret
`keys/search-secret` (web_search reads Google's page now), and `browser/pinned-tools.json` (the browser image is
pinned by digest instead)."""

from __future__ import annotations

from .common import accounts


def _leftovers(vault):
    return [
        *(a / "schedule.json" for a in accounts(vault)),
        vault.root / "keys" / "search-secret",
        vault.root / "browser" / "pinned-tools.json",
    ]


def needed(vault) -> bool:
    return any(p.exists() for p in _leftovers(vault))


def run(vault) -> None:
    for p in _leftovers(vault):
        p.unlink(missing_ok=True)
