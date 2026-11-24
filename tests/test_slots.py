# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Cache slots (SPEC §3.2): each active person keeps their own; whatever's done for them goes in theirs."""

from __future__ import annotations

import json

import httpx

from aha_local.llm import LLM, Slots


def test_each_person_keeps_a_slot_and_the_oldest_message_gives_it_up():
    last: dict[str, float] = {}
    s = Slots(3, last)
    last.update(fe=1.0, hb=2.0, ana=3.0)
    assert (s.of("fe"), s.of("hb"), s.of("ana")) == (0, 1, 2)  # every slot goes to a person
    assert s.of("fe") == 0  # kept
    last["zoe"] = 4.0
    assert s.of("zoe") == 0  # a new person always gets one: Fe's message was the oldest
    last["fe"] = 5.0  # Fe writes again
    assert s.of("fe") == 1  # and takes the slot of the oldest message now (Haibo's)
    assert Slots(1, {}).of("fe") is None  # one slot: the server chooses


async def test_requests_carry_their_persons_slot():
    sent: list[dict] = []

    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"total_slots": 4})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    llm = LLM("http://llm/v1", "m")
    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    llm.wrote("fe")
    await llm.chat([{"role": "user", "content": "hi"}], who="fe")
    llm.wrote("hb")
    await llm.chat([{"role": "user", "content": "hi"}], who="hb")
    await llm.chat([{"role": "user", "content": "the summary, asked at the end"}], who="fe")
    await llm.chat([{"role": "system", "content": "warm"}])  # no person: the server chooses
    assert [b.get("id_slot") for b in sent] == [0, 1, 0, None]
    llm.forget_slots()  # another model: its slots start empty
    await llm.chat([{"role": "user", "content": "hi"}], who="hb")
    assert sent[-1]["id_slot"] == 0
