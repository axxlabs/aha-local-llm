# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topic numbers and topic_read (SPEC §7.4 "Topic numbers")."""

import json

from aha_local import messages, numbers
from aha_local.migrations import m0010_part_numbers
from aha_local.tools import background
from aha_local.tools.conversation import topic_read
from aha_local.tools.registry import TaskState, ToolContext

from .conftest import person


def item(account, seq, **kw):
    account.transcript({"kind": "item", "item": {"seq": seq, "ts": 1764240000 + seq * 60, **kw}})


def chat(account):
    """Topic A (1-3), then a background task's note (4), then topic B (5-6) after a cut."""
    item(account, 1, t="msg", role="me", text="When is Natalia free?")
    item(account, 2, t="msg", role="aha", text="She said Sunday 18:00.")
    item(account, 3, t="title", **{"from": 1}, text="Natalia sync time")
    note = {"kind": "update", "title": "Reply from Sam", "id": "bg1"}
    item(account, 4, t="msg", role="aha", text="Sam replied: yes.", task=note)
    item(account, 5, t="title", **{"from": 4}, text="Reply from Sam")
    item(account, 6, t="cut")
    item(account, 7, t="msg", role="me", text="Weather tomorrow?")
    item(account, 8, t="title", **{"from": 7}, text="Weather", new=True)


def test_numbers_are_given_once_in_order_and_never_reused(accounts):
    fe = person(accounts)
    assert [numbers.part(fe, s) for s in (1, 4, 1, 7)] == [1, 2, 1, 3]
    assert numbers.start_of(fe, 2) == 4 and numbers.start_of(fe, 9) is None


def test_the_app_gets_each_titles_number_from_the_one_store(accounts):
    fe = person(accounts)
    chat(fe)
    numbers.part(fe, 1), numbers.part(fe, 4)
    titles = [it for it in fe.items() if it["t"] == "title"]
    assert [it.get("n") for it in titles] == [1, 2, None]  # topic 7 hasn't been titled through the channel
    raw = [json.loads(line)["item"] for line in fe.vault.records(fe.dir / "transcript.jsonl")]
    assert not any("n" in it for it in raw)  # never stored in the item


async def test_topic_read_gives_the_parts_messages_and_its_task(accounts, config):
    fe = person(accounts)
    chat(fe)
    for s in (1, 4, 7):
        numbers.part(fe, s)
    background.save(
        fe,
        [
            {
                "id": "bg1",
                "title": "Reply from Sam",
                "prompt": "Check email from Sam.",
                "done_when": "Sam replied",
                "every": 15,
                "next": 0,
                "created": 0,
            }
        ],
    )
    ctx = ToolContext(fe, None, config, TaskState())
    one = await topic_read({"number": 1}, ctx)
    assert one.text.startswith("Topic 1: Natalia sync time")
    assert "User: When is Natalia free?" in one.text and "Sam replied" not in one.text
    two = await topic_read({"number": "#2"}, ctx)
    assert "background task “Reply from Sam”" in two.text and "still running" in two.text
    three = await topic_read({"number": 3}, ctx)
    assert "Weather tomorrow?" in three.text and "Natalia" not in three.text
    assert (await topic_read({"number": 42}, ctx)).error


def test_the_migration_numbers_earlier_topics_oldest_first(accounts):
    fe = person(accounts)
    chat(fe)
    assert m0010_part_numbers.needed(fe.vault)
    m0010_part_numbers.run(fe.vault)
    assert numbers.parts(fe) == {1: 1, 4: 2, 7: 3} and numbers.part(fe, 20) == 4
    assert not m0010_part_numbers.needed(fe.vault)


def test_skill_and_topic_reads_are_said_plainly():
    assert messages.call_line("skill_read", {"name": "Bookings"}, False) == "Read skill bookings.md"
    assert messages.call_line("topic_read", {"number": 12}, False) == "Read topic 12"
