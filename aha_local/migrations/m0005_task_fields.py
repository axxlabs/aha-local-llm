# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Background tasks (SPEC §3.4), each kept in today's fields:
- permissions: "sites" (typed there) is "type", "calendar" is both "calendar_add" and "calendar_change";
- how the last check went: "unfinished" and "interrupted" are one "ended_early";
- "manual" (the running check was a Run now) is gone: "now" stays until the check ends.
"""

from __future__ import annotations

from .common import accounts

OLD_HOW = {"unfinished", "interrupted"}


def _old(entry: dict) -> bool:
    grant = entry.get("grant") if isinstance(entry.get("grant"), dict) else {}
    return "manual" in entry or entry.get("last_how") in OLD_HOW or "sites" in grant or "calendar" in grant


def _new(entry: dict) -> dict:
    entry = {k: v for k, v in entry.items() if k != "manual"}
    if entry.get("last_how") in OLD_HOW:
        entry["last_how"] = "ended_early"
    grant = dict(entry["grant"]) if isinstance(entry.get("grant"), dict) else None
    if grant is not None:
        if "sites" in grant:
            grant["type"] = sorted(set(grant.get("type") or []) | set(grant.pop("sites") or []))
        if "calendar" in grant:
            both = grant.pop("calendar") is True
            grant["calendar_add"] = grant.get("calendar_add") is True or both
            grant["calendar_change"] = grant.get("calendar_change") is True or both
        entry["grant"] = grant
    return entry


def _tasks(vault, account):
    path = account / "tasks.json"
    data = vault.read_json(path, None) if path.exists() else None
    return path, data if isinstance(data, list) else []


def needed(vault) -> bool:
    return any(isinstance(e, dict) and _old(e) for a in accounts(vault) for e in _tasks(vault, a)[1])


def run(vault) -> None:
    for account in accounts(vault):
        path, entries = _tasks(vault, account)
        if any(isinstance(e, dict) and _old(e) for e in entries):
            vault.write_json(path, [_new(e) if isinstance(e, dict) else e for e in entries])
