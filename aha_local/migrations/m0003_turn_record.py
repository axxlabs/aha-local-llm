# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""What a request did (SPEC §4): a turn's record, kept as "done" before 2026-11-26, is "record"."""

from __future__ import annotations

from .common import accounts


def _turns(vault, account):
    path = account / "context.json"
    conv = vault.read_json(path, None) if path.exists() else None
    return path, conv


def needed(vault) -> bool:
    for account in accounts(vault):
        _, conv = _turns(vault, account)
        if isinstance(conv, dict) and any("done" in t for t in conv.get("turns") or []):
            return True
    return False


def run(vault) -> None:
    for account in accounts(vault):
        path, conv = _turns(vault, account)
        if not isinstance(conv, dict):
            continue
        turns = conv.get("turns") or []
        if not any("done" in t for t in turns):
            continue
        for t in turns:
            if "done" in t:
                t["record"] = t.pop("done")
        vault.write_json(path, conv)
