# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""What a request did (SPEC §4): a later request sees only the messages, so what an earlier one changed is kept
with its answer, even when it stopped before saying so."""

import asyncio
from types import SimpleNamespace

from aha_local import messages
from aha_local.tools.browser import clicked_on
from aha_local.tools.registry import TaskState, Tool, ToolResult, params

from .conftest import person, say, settle


def earlier_answers(llm) -> list[str]:
    return [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "assistant" and m.get("content")]


async def test_a_fact_saved_before_the_request_stopped_is_known_to_the_next(agent, llm, chat, accounts):
    """Seen 2026-11-26: a fact saved, then the request ran on and stopped; its answer didn't say so, and the next
    two requests asked to save it again."""
    fe = person(accounts)
    release = asyncio.Event()

    async def slow(args, ctx):
        await release.wait()
        return ToolResult("done")

    agent.registry.add(Tool("slow", "", params(), slow))
    llm.call("memory_add", text="Checks order history before adding to the basket")
    llm.call("slow")
    await say(agent, "add to memory: check my order history first")
    ask = await chat.next()
    await say(agent, "✅ Save", button=ask.buttons[0].id)
    await asyncio.sleep(0.05)
    await agent.stop(fe)
    await settle(agent)
    [saved] = fe.load_memory()

    llm.answer("Hello.")
    await say(agent, "hi")
    await settle(agent)
    stopped = earlier_answers(llm)[-1]
    assert stopped.startswith(messages.STOPPED)
    assert f"- memory_add: Saved [{saved['id']}]: Checks order history before adding to the basket" in stopped
    assert "Carry on" not in stopped  # the advice to the model isn't part of what it did


async def test_what_was_declined_or_only_read_is_not_in_the_record(agent, llm, chat, accounts):
    async def look(args, ctx):
        return ToolResult("a page")

    agent.registry.add(Tool("look", "", params(), look))
    llm.call("look")
    llm.call("memory_add", text="Likes spicy food")
    llm.answer("OK.")
    await say(agent, "I like spicy food")
    ask = await chat.next()
    await say(agent, "❌ Don't", button=ask.buttons[-1].id)
    await settle(agent)
    assert "done" not in person(accounts).load_context()["turns"][-1]

    llm.answer("Hi.")
    await say(agent, "hi")
    await settle(agent)
    assert "record" not in earlier_answers(llm)[-1]


async def test_a_record_line_hides_what_was_typed_on_a_sign_in_card(agent, llm, chat, accounts):
    async def act(args, ctx):
        ctx.task.secrets.append("hunter22")
        return ToolResult("ok", did="Typed: “hunter22”.")

    agent.registry.add(Tool("act", "", params(), act))
    llm.call("act")
    llm.answer("Done.")
    await say(agent, "do it")
    await settle(agent)
    [line] = person(accounts).load_context()["turns"][-1]["done"]
    assert line.startswith("act: Typed:") and "hunter22" not in line


def test_a_click_is_recorded_with_the_pages_own_label_and_site():
    task = TaskState(page_url="https://www.waitrose.com/ecom/favourites")
    task.ref_lines = {"e12": '- button "Add to trolley" [ref=e12]'}
    ctx = SimpleNamespace(task=task)
    assert clicked_on({"target": "e12", "element": "add button"}, ctx) == "Clicked “Add to trolley” on waitrose.com"
    assert clicked_on({"target": "e99", "element": "the  foil"}, ctx) == "Clicked “the foil” on waitrose.com"
