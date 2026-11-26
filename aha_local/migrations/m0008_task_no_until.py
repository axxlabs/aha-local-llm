# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Background tasks have no end date (2026-11-26: "remove until and all the code"): what ends one is its
`done_when`, or the person. A task's `until` goes."""

from __future__ import annotations

from .common import accounts


def _tasks(vault, account):
    path = account / "tasks.json"
    data = vault.read_json(path, None) if path.exists() else None
    return path, data if isinstance(data, list) else []


def needed(vault) -> bool:
    return any(isinstance(e, dict) and "until" in e for a in accounts(vault) for e in _tasks(vault, a)[1])


def run(vault) -> None:
    for account in accounts(vault):
        path, entries = _tasks(vault, account)
        if any(isinstance(e, dict) and "until" in e for e in entries):
            kept = [{k: v for k, v in e.items() if k != "until"} if isinstance(e, dict) else e for e in entries]
            vault.write_json(path, kept)
