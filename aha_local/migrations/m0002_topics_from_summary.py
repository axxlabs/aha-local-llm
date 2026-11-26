# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topics (SPEC §4): the rolling summary of before them becomes one topic entry, "Earlier conversation"."""

from __future__ import annotations

import datetime as dt

from .common import accounts


def _context(vault, account):
    path = account / "context.json"
    return path, (vault.read_json(path, None) if path.exists() else None)


def needed(vault) -> bool:
    return any(isinstance(c, dict) and "summary" in c for c in (_context(vault, a)[1] for a in accounts(vault)))


def _epoch(stamp) -> float:
    try:
        return dt.datetime.strptime(str(stamp), "%Y-%m-%d %H:%M").timestamp()
    except ValueError:
        return 0


def run(vault) -> None:
    for account in accounts(vault):
        path, conv = _context(vault, account)
        if not isinstance(conv, dict) or "summary" not in conv:
            continue
        summary, since = conv.pop("summary"), conv.pop("summary_from", None)
        topics = conv.get("topics") or []
        if summary:
            entry = {"id": "t0", "from": 0, "title": "Earlier conversation", "text": summary, "ts": _epoch(since)}
            topics = [entry, *topics]
        vault.write_json(path, {**conv, "turns": conv.get("turns") or [], "topics": topics})
