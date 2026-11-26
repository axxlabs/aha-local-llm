# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Background tasks (SPEC §3.4) say what ends them, `done_when` (2026-11-26: a check that found the reply reported
it as news and the task went on). A task from before has none: it's "never", until the person cancels it, as its
checks treated it."""

from __future__ import annotations

from .common import accounts


def _tasks(vault, account):
    path = account / "tasks.json"
    data = vault.read_json(path, None) if path.exists() else None
    return path, data if isinstance(data, list) else []


def needed(vault) -> bool:
    return any(isinstance(e, dict) and "done_when" not in e for a in accounts(vault) for e in _tasks(vault, a)[1])


def run(vault) -> None:
    for account in accounts(vault):
        path, entries = _tasks(vault, account)
        if any(isinstance(e, dict) and "done_when" not in e for e in entries):
            vault.write_json(
                path,
                [{**e, "done_when": "never"} if isinstance(e, dict) and "done_when" not in e else e for e in entries],
            )
