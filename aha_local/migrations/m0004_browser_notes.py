# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The browser notes (SPEC §3.4) keep only the page that was open last: version 1's exported copy of the
browser's storage and version 2's signed-in sites go (a site is signed in when its page says so), and so does
`sign-ins.json`, from before the browser kept everything in its own profile."""

from __future__ import annotations

from .common import accounts


def needed(vault) -> bool:
    for account in accounts(vault):
        if (account / "sign-ins.json").exists():
            return True
        path = account / "browser.json"
        data = vault.read_json(path, None) if path.exists() else None
        if isinstance(data, dict) and set(data) - {"v", "page"}:
            return True
    return False


def run(vault) -> None:
    for account in accounts(vault):
        (account / "sign-ins.json").unlink(missing_ok=True)
        path = account / "browser.json"
        data = vault.read_json(path, None) if path.exists() else None
        if not isinstance(data, dict):
            continue
        page = data.get("page")
        if isinstance(page, str) and page:
            vault.write_json(path, {"v": 3, "page": page})
        else:
            path.unlink()
