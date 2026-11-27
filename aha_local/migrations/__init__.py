# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The data folder's versions (SPEC §3.8.3 "Storage versions"), like a database's migrations.

Every change to what's stored is one numbered script here, `mNNNN_<what>.py`, with:
  - `needed(vault)`: whether this folder still has the old shape (so a folder that never had it skips the work);
  - `run(vault)`: change it, idempotent, so a crash halfway is finished at the next start.

The folder's version, the number of the last script applied, is in `format.json` (unencrypted, next to the
`rest` marker). At each start, before anything reads the folder, the scripts above that number run in order and
the version moves on after each one. A folder from a newer build (a higher version than the last script here) is
refused rather than read wrong. The code reads only today's shape: anything older is a script's job, never a
fallback in the code that reads it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    m0001_encrypt_at_rest,
    m0002_topics_from_summary,
    m0003_turn_record,
    m0004_browser_notes,
    m0005_task_fields,
    m0006_leftover_files,
    m0007_task_done_when,
    m0008_task_no_until,
    m0009_task_no_grant,
    m0010_part_numbers,
)

if TYPE_CHECKING:
    from ..vault import Vault

log = logging.getLogger("aha.migrations")

FORMAT_FILE = "format.json"


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    needed: Callable[[Vault], bool]
    run: Callable[[Vault], None]


def _of(module) -> Migration:
    stem = module.__name__.rsplit(".", 1)[-1]  # mNNNN_<what>
    return Migration(int(stem[1:5]), stem[6:], module.needed, module.run)


MIGRATIONS = tuple(
    _of(m)
    for m in (
        m0001_encrypt_at_rest,
        m0002_topics_from_summary,
        m0003_turn_record,
        m0004_browser_notes,
        m0005_task_fields,
        m0006_leftover_files,
        m0007_task_done_when,
        m0008_task_no_until,
        m0009_task_no_grant,
        m0010_part_numbers,
    )
)
LATEST = MIGRATIONS[-1].number


class NewerData(Exception):
    """The data folder was written by a newer build than this one."""


def version(root: Path) -> int:
    marker = root / FORMAT_FILE
    return int(json.loads(marker.read_text()).get("version", 0)) if marker.exists() else 0


def _set_version(root: Path, number: int) -> None:
    marker = root / FORMAT_FILE
    data = json.loads(marker.read_text()) if marker.exists() else {}
    tmp = marker.with_name(FORMAT_FILE + ".tmp")
    tmp.write_text(json.dumps({**data, "rest": 1, "version": number}) + "\n")
    tmp.replace(marker)


def upgrade(vault: Vault) -> list[str]:
    """Bring the folder to LATEST. Returns the names of the scripts that changed something."""
    vault.root.mkdir(parents=True, exist_ok=True)
    at = version(vault.root)
    if at > LATEST:
        raise NewerData(f"the data folder is at storage version {at}; this build of Aha! knows up to {LATEST}")
    ran = []
    for m in MIGRATIONS:
        if m.number <= at:
            continue
        if m.needed(vault):
            log.info("storage migration %04d (%s)", m.number, m.name)
            m.run(vault)
            ran.append(m.name)
        _set_version(vault.root, m.number)
    return ran
