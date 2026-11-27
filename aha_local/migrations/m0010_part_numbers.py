# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topic numbers (SPEC §7.4 "Topic numbers"): the parts titled before numbers existed get theirs, oldest first,
so the person can refer to any of them. `numbers.json` is written for each account that has none."""

from __future__ import annotations

import json

from .common import accounts

FILE = "numbers.json"


def needed(vault) -> bool:
    return any(not (a / FILE).exists() for a in accounts(vault))


def _starts(vault, account) -> list[int]:
    path = account / "transcript.jsonl"
    starts: set[int] = set()
    for line in vault.records(path) if path.exists() else []:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = e.get("item") if isinstance(e, dict) and e.get("kind") == "item" else None
        if isinstance(item, dict) and item.get("t") == "title" and isinstance(item.get("from"), int):
            starts.add(item["from"])
    return sorted(starts)


def run(vault) -> None:
    for account in accounts(vault):
        if (account / FILE).exists():
            continue
        starts = _starts(vault, account)
        parts = {str(s): i for i, s in enumerate(starts, 1)}
        vault.write_json(account / FILE, {"next": len(starts) + 1, "parts": parts})
