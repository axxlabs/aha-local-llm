# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""History length and Clear history (SPEC §3.8.1, §3.8.2)."""

from __future__ import annotations

import datetime as dt
import json
import os
import time

from aha_local import context
from aha_local.accounts import Accounts
from aha_local.e2e import crypto as c
from aha_local.protocol import E2E
from aha_local.rest import Vault

from .test_channel import Rig

DAY = 86400


def by_device(rig, *devices) -> dict[str, list[dict]]:
    """Each device's decrypted messages (test_channel's inbox() expects one device)."""
    out: dict[str, list[dict]] = {d.dev: [] for d in devices}
    frames = rig.take(E2E)
    for d in devices:
        for f in [f for f in frames if f.dev == d.dev]:
            out[d.dev].append(c.decrypt_json(d.ratchet, f.box))
    return out


def old_item(acct, text: str, days_ago: float) -> None:
    acct.transcript(
        {
            "kind": "item",
            "item": {
                "t": "msg",
                "role": "aha",
                "text": text,
                "seq": acct.next_seq(),
                "ts": int(time.time() - days_ago * DAY),
            },
        }
    )


def local(days_ago: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return (dt.datetime.now() - dt.timedelta(days=days_ago)).strftime(fmt)


def test_a_rewritten_log_reads_back_and_takes_appends(tmp_path):
    vault = Vault(tmp_path, os.urandom(32))
    log = tmp_path / "a" / "transcript.jsonl"
    for i in range(4):
        vault.append(log, f"r{i}".encode())
    vault.rewrite(log, [b"r2", b"r3"])
    vault.append(log, b"r4")
    assert vault.records(log) == [b"r2", b"r3", b"r4"]
    assert Vault(tmp_path, vault._key).records(log) == [b"r2", b"r3", b"r4"]  # as read after a restart
    vault.rewrite(log, [])
    assert not log.exists() and vault.records(log) == []


def test_a_length_deletes_older_history_everywhere_on_the_computer(tmp_path):
    accounts = Accounts(Vault(tmp_path, os.urandom(32)))
    acct = accounts.create("Ana")
    old_item(acct, "from two weeks ago", 14)
    old_item(acct, "from yesterday", 1)
    acct.audit({"ts": local(14, "%Y-%m-%dT%H:%M:%S%z"), "kind": "approval", "title": "old"})
    acct.audit({"kind": "approval", "title": "new"})
    acct.save_context(
        {
            "summary": "talked about a fictional trip",
            "summary_from": local(10),
            "turns": [
                {"role": "user", "content": "old question", "ts": local(9)},
                {"role": "assistant", "content": "old answer", "ts": local(9)},
                {"role": "user", "content": "new question", "ts": local(0.1)},
                {"role": "assistant", "content": "new answer", "ts": local(0.1)},
            ],
        }
    )
    assert acct.prune_history() is False  # forever: nothing goes

    acct.history_days = 7
    acct.save()
    assert acct.prune_history() is True
    assert [e["item"]["text"] for e in acct.read_transcript()] == ["from yesterday"]
    assert [json.loads(r)["title"] for r in acct.vault.records(acct.dir / "audit.jsonl")] == ["new"]
    conv = acct.load_context()
    assert [t["content"] for t in conv["turns"]] == ["new question", "new answer"]
    assert conv["summary"] == "" and "summary_from" not in conv  # it held turns older than a week
    assert acct.history_start() == 2
    assert acct.prune_history() is False  # nothing more to do

    again = Accounts(acct.vault).get(acct.id)
    assert again.history_days == 7  # kept in account.json


def test_a_summary_is_kept_while_its_turns_are_within_the_length(tmp_path):
    accounts = Accounts(Vault(tmp_path, os.urandom(32)))
    acct = accounts.create("Ana")
    acct.history_days = 30
    acct.save_context({"summary": "recent things", "summary_from": local(3), "turns": []})
    acct.prune_history()
    assert acct.load_context()["summary"] == "recent things"
    acct.save_context({"summary": "from before dates were kept", "turns": []})
    acct.prune_history()
    assert acct.load_context()["summary"] == ""  # undated: its age is unknown, so it goes


def test_a_fold_records_the_summarys_age_and_a_fold_after_a_clear_is_dropped(tmp_path):
    accounts = Accounts(Vault(tmp_path, os.urandom(32)))
    acct = accounts.create("Ana")
    turns = [{"role": "user", "content": "q", "ts": local(5)}, {"role": "assistant", "content": "a", "ts": local(5)}]
    acct.save_context({"summary": "", "turns": list(turns)})
    context.apply_fold(acct, turns, "they asked q")
    assert acct.load_context()["summary_from"] == local(5)

    acct.save_context({"summary": "", "turns": list(turns)})
    acct.clear_history()  # while the summary was being written
    context.apply_fold(acct, turns, "they asked q")
    assert acct.load_context() == {"summary": "", "turns": []}


async def test_settings_and_clear_reach_every_device(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "Phone"})
    _, laptop = await rig.pair()
    await laptop.send({"t": "hello", "name": "Laptop"})
    acct = rig.accounts.find("Ana")
    old_item(acct, "a fortnight ago", 14)
    await rig.channel.message(acct, "today")
    by_device(rig, phone, laptop)

    await phone.send({"t": "history_settings_request"})
    assert by_device(rig, phone, laptop)[phone.dev] == [{"t": "history_settings", "days": None}]

    await phone.send({"t": "history_settings_set", "days": 7})
    got = by_device(rig, phone, laptop)
    start = acct.history_start()
    for dev in (phone.dev, laptop.dev):
        assert got[dev] == [{"t": "history_settings", "days": 7}, {"t": "history_start", "start": start}]
    texts = [e["item"]["text"] for e in acct.read_transcript() if e["kind"] == "item"]
    assert "a fortnight ago" not in texts and "today" in texts

    await phone.send({"t": "history_settings_set", "days": 2})  # not a choice
    assert by_device(rig, phone, laptop)[phone.dev] == [{"t": "error", "code": "bad_value"}]
    assert acct.history_days == 7

    seq = acct.seq
    await laptop.send({"t": "history_clear"})
    assert rig.agent.stopped == 1 and rig.agent.quiet is True
    got = by_device(rig, phone, laptop)
    for dev in (phone.dev, laptop.dev):
        assert got[dev] == [{"t": "history_start", "start": seq + 1}]
    assert acct.read_transcript() == [] and not (acct.dir / "context.json").exists()

    await rig.channel.message(acct, "after the clear")
    by_device(rig, phone, laptop)
    await phone.send({"t": "sync_request", "after": 0})
    sync = by_device(rig, phone, laptop)[phone.dev][0]
    assert [x["seq"] for x in sync["items"]] == [seq + 1] and sync["start"] == seq + 1  # numbers carry on
