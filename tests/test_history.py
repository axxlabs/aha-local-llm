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
from aha_local.vault import Vault

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
            "topics": [
                {"id": "t1", "from": 1, "title": "Trip", "text": "a fictional trip", "ts": time.time() - 10 * 86400},
                {"id": "t2", "from": 2, "title": "Dinner", "text": "a fictional dinner", "ts": time.time() - 86400},
            ],
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
    assert [e["id"] for e in conv["topics"]] == ["t2"]  # the topic memory follows the length too (SPEC §5.4)
    assert acct.history_start() == 2
    assert acct.prune_history() is False  # nothing more to do

    again = Accounts(acct.vault).get(acct.id)
    assert again.history_days == 7  # kept in account.json


def test_an_old_summary_becomes_a_topic_entry_and_follows_the_length(tmp_path):
    """Before topics (SPEC §4): the rolling summary becomes one entry, dated by its oldest turn."""
    accounts = Accounts(Vault(tmp_path, os.urandom(32)))
    acct = accounts.create("Ana")
    acct.history_days = 30
    acct.save_context({"summary": "recent things", "summary_from": local(3), "turns": []})
    acct.prune_history()
    [entry] = acct.load_context()["topics"]
    assert entry["title"] == "Earlier conversation" and entry["text"] == "recent things"
    acct.save_context({"summary": "from before dates were kept", "turns": []})
    acct.prune_history()
    assert acct.load_context()["topics"] == []  # undated: its age is unknown, so it goes


def test_a_topic_entry_replaces_its_earlier_one_and_earlier_topics_turns_leave(tmp_path):
    accounts = Accounts(Vault(tmp_path, os.urandom(32)))
    acct = accounts.create("Ana")
    now = time.time()
    old = {"role": "user", "content": "old topic", "t": now - 3 * 3600}
    acct.save_context({"turns": [old], "topics": []})
    context.save_topic(acct, 5, now - 60, "Parcel", "Tracking H01")
    context.save_topic(acct, 5, now - 60, "Evri parcel", "Tracking H01, collect 26/11/2026")
    conv = acct.load_context()
    assert conv["topics"] == [
        {"id": "t5", "from": 5, "title": "Evri parcel", "text": "Tracking H01, collect 26/11/2026", "ts": now - 60}
    ]
    assert conv["turns"] == []  # the earlier topic's turns left the context; its entry stays
    context.save_topic(acct, 5, now - 60, "Evri parcel", "")  # nothing worth keeping: no entry
    assert acct.load_context()["topics"] == []
    acct.clear_history()
    assert acct.load_context() == {"turns": [], "topics": []}


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


def test_every_fact_is_in_view_and_earlier_topics_are_searched(tmp_path):
    """SPEC §5.4: every approved fact, however many, in every request; the topics' entries aren't, and
    conversation_search finds them, best match first."""
    import asyncio
    from types import SimpleNamespace

    from aha_local.tools import memory
    from aha_local.tools.conversation import conversation_search
    from aha_local.tools.registry import Registry

    accounts = Accounts(Vault(tmp_path, os.urandom(32)))
    acct = accounts.create("Ana")
    for i in range(80):
        context.save_topic(acct, 100 + i, time.time() - (80 - i) * 60, f"Topic {i}", f"Tracking CODE{i:03d}")
        memory.add_fact(acct, f"Fact {i}")
    block = context.build(acct, [], "Europe/London", "Home", 100_000)[1]["content"]
    assert all(f"Fact {i}" in block for i in range(80)) and "CODE" not in block
    assert "conversation_search" in block
    found = asyncio.run(conversation_search({"query": "tracking code042"}, SimpleNamespace(account=acct))).text
    assert found.index("CODE042") < found.index("CODE079")  # the best match first, then the newest
    reg = Registry()
    memory.register(reg)
    assert "memory_search" not in reg.names() and "memory_list" not in reg.names()


def test_the_date_comes_before_the_memory(tmp_path):
    """SPEC §4: the model reads every fact knowing what day it is."""
    import datetime as dt

    from aha_local.tools import memory

    accounts = Accounts(Vault(tmp_path, os.urandom(32)))
    acct = accounts.create("Ana")
    memory.add_fact(acct, "Lives in Lisbon")
    block = context.context_block(acct, "Europe/London", "Home", dt.datetime(2026, 11, 25, 9, 0))
    assert block.index("Today: 25/11/2026") < block.index("What you remember") < block.index("Lisbon")
