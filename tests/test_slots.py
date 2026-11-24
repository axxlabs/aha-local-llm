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


async def test_a_task_keeps_the_model_between_its_steps_and_frees_it_while_a_card_waits():
    """SPEC §3.3: one task at a time, not one step at a time (with the hybrid model, only the last-used slot keeps
    its cache: steps taking turns re-read their whole prompt every time)."""
    import asyncio

    llm = LLM("http://llm/v1", "m")
    llm.slots = Slots(0, {})
    order: list[str] = []

    async def post(body):
        order.append(body["messages"][0]["content"])
        await asyncio.sleep(0.01)
        return {"choices": [{"message": {"content": "ok"}}]}

    llm._post = post
    card = asyncio.Event()

    async def fe_task():
        async with llm.turn("fe"):
            await llm.chat([{"role": "user", "content": "fe 1"}], who="fe")
            await asyncio.sleep(0.03)  # a browser step: the model stays Fe's
            await llm.chat([{"role": "user", "content": "fe 2"}], who="fe")
            async with llm.paused("fe"):  # a card: Haibo may go meanwhile
                await card.wait()
            await llm.chat([{"role": "user", "content": "fe 3"}], who="fe")

    async def hb_task():
        await asyncio.sleep(0.005)
        async with llm.turn("hb"):
            assert not llm.queued("fe")
            await llm.chat([{"role": "user", "content": "hb 1"}], who="hb")
            card.set()  # Fe answers the card while Haibo's task runs
            await asyncio.sleep(0.02)
            await llm.chat([{"role": "user", "content": "hb 2"}], who="hb")

    fe, hb = asyncio.create_task(fe_task()), asyncio.create_task(hb_task())
    await asyncio.sleep(0.02)
    assert llm.queued("hb")  # Haibo's task waits for Fe's: the app says so
    await asyncio.gather(fe, hb)
    assert order == ["fe 1", "fe 2", "hb 1", "hb 2", "fe 3"]  # no step of one between steps of the other
    assert llm.turn_owner is None and llm.holder is None and not llm.lock.locked()


async def test_a_stopped_task_gives_the_model_back():
    import asyncio

    llm = LLM("http://llm/v1", "m")
    llm.slots = Slots(0, {})

    async def task():
        async with llm.turn("fe"), llm.paused("fe"):
            await asyncio.sleep(10)  # waiting on a card when stopped

    t = asyncio.create_task(task())
    await asyncio.sleep(0.01)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    assert not llm.lock.locked() and llm.turn_owner is None


async def test_a_task_stopped_on_a_card_does_not_wait_for_another_task():
    import asyncio

    llm = LLM("http://llm/v1", "m")
    llm.slots = Slots(0, {})
    other = asyncio.Event()

    async def fe():
        async with llm.turn("fe"), llm.paused("fe"):
            await asyncio.sleep(10)

    async def hb():
        async with llm.turn("hb"):
            await other.wait()  # Haibo's long task holds the model

    f = asyncio.create_task(fe())
    await asyncio.sleep(0.01)
    h = asyncio.create_task(hb())
    await asyncio.sleep(0.01)
    f.cancel()
    await asyncio.wait_for(asyncio.gather(f, return_exceptions=True), 1)  # stops at once
    other.set()
    await h
    assert not llm.lock.locked()
