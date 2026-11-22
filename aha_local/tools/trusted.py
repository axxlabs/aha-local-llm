# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The sites a person trusts to fill in forms without asking (SPEC §5.7).

Set only by the person, from Settings (e2e/channel.py): no tool, argument or message from the model reaches this
module's writers. Kept encrypted in the account's own folder.
"""

from __future__ import annotations

import re

from ..accounts import Account
from ..policy.urls import check_url
from .signins import site_name

FILE = "trusted.json"
MAX_SITES = 200


def sites(account: Account) -> list[str]:
    data = account.vault.read_json(account.dir / FILE, None) if (account.dir / FILE).exists() else None
    found = data.get("sites", []) if isinstance(data, dict) else []
    return sorted({s for s in found if isinstance(s, str) and s})


def _save(account: Account, found: list[str]) -> None:
    path = account.dir / FILE
    if found:
        account.vault.write_json(path, {"sites": sorted(set(found))})
    else:
        path.unlink(missing_ok=True)


def valid(text: str) -> str:
    """The site as a registrable domain, if it names a public web site by name ('' otherwise: no addresses)."""
    host = text.strip().lower().removeprefix("https://").removeprefix("http://").split("/", 1)[0].split(":", 1)[0]
    if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,63}", host):
        return ""
    _, why = check_url(f"https://{host}/")
    return "" if why else site_name(host)


def set_trusted(account: Account, text: str, on: bool) -> bool:
    """Trust or untrust a site. False if it isn't a valid site (or the list is full)."""
    site = valid(text)
    if not site:
        return False
    current = sites(account)
    if on and site not in current:
        if len(current) >= MAX_SITES:
            return False
        current.append(site)
    elif not on:
        current = [s for s in current if s != site]
    _save(account, current)
    return True
