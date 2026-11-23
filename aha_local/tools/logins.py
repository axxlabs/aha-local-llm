# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Logins the person chose to save on a login card (SPEC §5.8).

Written only by browser_login, after the person ticked "Save this login on this computer" and the site let them
in; removed from Settings. Kept encrypted in the account's own folder, per exact host. The model never sees
what's in it: values from here are typed into the page by the harness and masked in every result.
"""

from __future__ import annotations

import time

from ..accounts import Account
from ..policy.urls import site_of

FILE = "logins.json"
MAX_LOGINS = 200


def _load(account: Account) -> dict[str, dict]:
    path = account.dir / FILE
    data = account.vault.read_json(path, None) if path.exists() else None
    found = data.get("logins", {}) if isinstance(data, dict) else {}
    return {h: v for h, v in found.items() if isinstance(v, dict) and isinstance(v.get("password"), str)}


def _save(account: Account, found: dict[str, dict]) -> None:
    path = account.dir / FILE
    if found:
        account.vault.write_json(path, {"logins": found})
    else:
        path.unlink(missing_ok=True)


def get(account: Account, host: str) -> dict | None:
    """{"username", "password"} for this exact host, if saved."""
    return _load(account).get(host.lower().removeprefix("www."))


def save(account: Account, host: str, username: str, password: str) -> None:
    found = _load(account)
    host = host.lower().removeprefix("www.")
    if host not in found and len(found) >= MAX_LOGINS:
        return
    found[host] = {"username": username, "password": password, "saved": int(time.time())}
    _save(account, found)


def sites(account: Account) -> set[str]:
    """The sites (registrable domains) with a saved login, for Settings: never the values."""
    return {site_of(h) for h in _load(account)}


def forget(account: Account, site: str) -> bool:
    """Forget every saved login on this site. False if there was none."""
    found = _load(account)
    kept = {h: v for h, v in found.items() if site_of(h) != site}
    if len(kept) == len(found):
        return False
    _save(account, kept)
    return True


def clear(account: Account) -> None:
    (account.dir / FILE).unlink(missing_ok=True)
