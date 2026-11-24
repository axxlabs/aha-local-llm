# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Photos from the person (SPEC §6.5 `say.image`, §3.5.1 "Photos")."""

from __future__ import annotations

import base64

from aha_local.items import Image

from .conftest import person, settle

RECEIPT = Image("image/jpeg", base64.b64encode(b"\xff\xd8\xff a fictional receipt").decode())


def images_in(request: dict) -> list[str]:
    return [
        part["image_url"]["url"]
        for m in request["messages"]
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if part.get("type") == "image_url"
    ]


async def test_a_photo_goes_to_the_model_and_its_numbers_dont_trigger_the_check(agent, llm, chat, accounts):
    fe = person(accounts)
    llm.answer("The total on the receipt is £23.40, paid at 14:05.")
    await agent.inbound(fe, "how much did I spend?", RECEIPT)
    await settle(agent)
    assert images_in(llm.requests[0]) == [f"data:image/jpeg;base64,{RECEIPT.b64}"]
    assert len(llm.requests) == 1  # no retry for numbers read from the photo
    assert chat.texts() == ["The total on the receipt is £23.40, paid at 14:05."]  # and no warning
    (user, answer) = fe.load_context()["turns"][-2:]
    assert user["content"] == "how much did I spend? [photo]" and answer["checked"] is False
    assert any(e.get("image") for e in fe.read_transcript() if e["kind"] == "user")


async def test_a_photo_alone_starts_a_task_and_is_never_a_command(agent, llm, chat, accounts):
    fe = person(accounts)
    llm.answer("It's a receipt from a fictional bistro.")
    await agent.inbound(fe, "stop", RECEIPT)  # the words of a command, with a photo: still a message
    await settle(agent)
    assert chat.texts() == ["It's a receipt from a fictional bistro."]
    llm.answer("Same bistro.")
    await agent.inbound(fe, "", RECEIPT)
    await settle(agent)
    first = llm.requests[-1]["messages"]
    assert any(isinstance(m["content"], list) and m["content"][0]["text"] == "(sent a photo)" for m in first)


async def test_a_photo_sent_while_working_reaches_the_running_task(agent, llm, chat, accounts):
    import asyncio

    fe = person(accounts)
    gate = asyncio.Event()
    real_chat = llm.chat

    async def slow(messages, tools=None, max_tokens=4096, who=None, **kw):
        if len(llm.requests) == 0:
            await gate.wait()
        return await real_chat(messages, tools, max_tokens, who, **kw)

    llm.chat = slow
    llm.call("current_time")
    llm.answer("Got the photo of the menu too.")
    await agent.inbound(fe, "what should I order?")
    await asyncio.sleep(0.01)
    await agent.inbound(fe, "here's the menu", RECEIPT)
    gate.set()
    await settle(agent)
    assert len(images_in(llm.requests[-1])) == 1
    assert "here's the menu" in str(llm.requests[-1]["messages"])
