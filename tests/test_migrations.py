# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Storage versions (SPEC §3.8.3): numbered scripts, each run once when a folder needs it, in order."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aha_local import migrations
from aha_local.accounts import Accounts
from aha_local.migrations import LATEST, MIGRATIONS, NewerData, upgrade, version
from aha_local.vault import Vault


def vault(tmp_path: Path) -> Vault:
    key = tmp_path.parent / f"{tmp_path.name}-key"
    key.write_bytes(os.urandom(32))
    return Vault.open(tmp_path, key)


def test_the_scripts_are_numbered_one_after_another_and_named_by_their_files():
    assert [m.number for m in MIGRATIONS] == list(range(1, LATEST + 1))
    files = sorted(p.stem for p in Path(migrations.__file__).parent.glob("m[0-9][0-9][0-9][0-9]_*.py"))
    assert files == [f"m{m.number:04d}_{m.name}" for m in MIGRATIONS]  # none forgotten in the list


def test_a_new_folder_is_at_the_latest_version_and_nothing_runs_twice(tmp_path):
    v = vault(tmp_path)
    assert version(tmp_path) == LATEST
    assert upgrade(v) == []


def test_a_folder_from_a_newer_build_is_refused(tmp_path):
    vault(tmp_path)
    marker = tmp_path / "format.json"
    marker.write_text(json.dumps({"rest": 1, "version": LATEST + 1}))
    with pytest.raises(NewerData):
        upgrade(Vault(tmp_path, os.urandom(32)))


def at_version(tmp_path: Path, n: int):
    """A folder written by the build whose storage version was `n`."""
    v = vault(tmp_path)
    (tmp_path / "format.json").write_text(json.dumps({"rest": 1, "version": n}))
    return v, Accounts(v).create("Ana")


def test_old_shapes_are_brought_to_todays(tmp_path):
    v, acct = at_version(tmp_path, 1)
    acct.save_context(
        {
            "summary": "recent things",
            "summary_from": "2026-11-20 10:00",
            "turns": [{"role": "assistant", "content": "Saved.", "done": ["memory_add: Saved"]}],
        }
    )
    v.write_json(acct.dir / "browser.json", {"v": 2, "signed_in": {"waitrose.com": {}}, "page": "https://x.example/"})
    (acct.dir / "sign-ins.json").write_text("{}")
    v.write_json(
        acct.dir / "tasks.json",
        [
            {
                "id": "t1",
                "manual": True,
                "last_how": "interrupted",
                "grant": {"sites": ["royalmail.com"], "calendar": True},
            }
        ],
    )
    (acct.dir / "schedule.json").write_text("[]")
    (tmp_path / "keys").mkdir(exist_ok=True)
    (tmp_path / "keys" / "search-secret").write_text("ab" * 32)

    ran = upgrade(v)
    assert ran == [
        "topics_from_summary",
        "turn_record",
        "browser_notes",
        "task_fields",
        "leftover_files",
        "task_done_when",
        "task_no_grant",
    ]
    assert version(tmp_path) == LATEST
    conv = acct.load_context()
    assert "summary" not in conv and conv["topics"][0]["title"] == "Earlier conversation"
    assert conv["turns"][0]["record"] == ["memory_add: Saved"] and "done" not in conv["turns"][0]
    assert v.read_json(acct.dir / "browser.json", None) == {"v": 3, "page": "https://x.example/"}
    [task] = v.read_json(acct.dir / "tasks.json", None)
    assert "manual" not in task and task["last_how"] == "ended_early"
    assert task["done_when"] == "never"  # a task from before: it went on until cancelled
    assert "grant" not in task  # no up-front permissions any more
    for gone in (acct.dir / "sign-ins.json", acct.dir / "schedule.json", tmp_path / "keys" / "search-secret"):
        assert not gone.exists(), gone
    assert upgrade(v) == []  # once
