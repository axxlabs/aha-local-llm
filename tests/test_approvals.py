# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""SPEC §5.3, §11.2: approve, edit, decline, free-text decline, stop, expiry; what runs is what was approved."""

import asyncio
import json

from aha_local.tools.registry import Tool, params

from .conftest import person, say, settle


def add_typing_tool(registry, ran: list):
    """A stand-in for a browser tool that needs approval, with the same flow."""
    from aha_local.policy.browser import form_for

    async def handler(args, ctx):
        form = form_for("browser_fill_form", args, "example.com")
        d = await ctx.approvals.ask(ctx.account, "action", form.title, form.fields)
        if d.reason == "expired":
            from aha_local.tools.registry import ToolResult

            return ToolResult("", error=True, kind="expired")
        from aha_local.tools.registry import ToolResult

        if not d.approved:
            from aha_local.tools import guard

            return guard.stopped(d)
        final = form.apply("browser_fill_form", args, d.values)
        ran.append(final)
        return ToolResult("filled")

    registry.add(Tool("browser_fill_form", "fill", params(), handler))


FORM = {
    "fields": [
        {"name": "Name", "type": "textbox", "target": "e1", "value": "Felipe"},
        {"name": "Phone", "type": "textbox", "target": "e2", "value": "07700 900123"},
    ]
}


async def start_form_task(agent, llm, chat, registry, ran):
    add_typing_tool(registry, ran)
    llm.call("browser_fill_form", **FORM)
    llm.answer("Booked.")
    await say(agent, "book it")
    msg = await chat.next()
    assert msg.card["title"] == "Fill in 2 fields on example.com?"
    assert [(f["label"], f["value"]) for f in msg.card["fields"]] == [("Name", "Felipe"), ("Phone", "07700 900123")]
    assert [b.label for b in msg.buttons] == ["Approve", "Edit", "Decline"]
    return msg


async def test_approve_runs_exactly_what_was_shown(agent, llm, chat, registry):
    ran = []
    msg = await start_form_task(agent, llm, chat, registry, ran)
    await say(agent, "✅ Approve", button=msg.buttons[0].id)
    await settle(agent)
    assert ran == [FORM]
    assert chat.texts()[-1] == "Booked."


async def test_values_edited_in_the_card_are_what_runs(agent, llm, chat, registry, accounts):
    ran = []
    msg = await start_form_task(agent, llm, chat, registry, ran)
    await agent.approval_reply(person(accounts), msg.card["id"], "approve", ["Felipe", "07700 900456"])
    await settle(agent)
    assert ran[0]["fields"][1]["value"] == "07700 900456"
    assert ran[0]["fields"][0]["value"] == "Felipe"


async def test_a_message_while_a_card_waits_cancels_it_and_comes_next(agent, llm, chat, registry):
    """The model never knows a card exists: the call it made isn't done, and the person's message follows it."""
    ran = []
    await start_form_task(agent, llm, chat, registry, ran)
    await say(agent, "no, use my work phone")
    await settle(agent)
    assert ran == [] and chat.states[-1][1] == "declined"
    msgs = llm.requests[-1]["messages"]
    tool = [i for i, m in enumerate(msgs) if m["role"] == "tool"][-1]
    assert msgs[tool]["content"].startswith("This wasn't done: the user didn't allow it. They sent a new message")
    assert msgs[tool + 1]["role"] == "user" and "no, use my work phone" in msgs[tool + 1]["content"]


async def test_decline_button(agent, llm, chat, registry):
    ran = []
    msg = await start_form_task(agent, llm, chat, registry, ran)
    await say(agent, "❌ Decline", button=msg.buttons[2].id)
    await settle(agent)
    assert ran == []


async def test_stop_cancels_the_task_and_its_approval(agent, llm, chat, registry):
    ran = []
    await start_form_task(agent, llm, chat, registry, ran)
    await say(agent, "stop")
    await settle(agent)
    assert ran == [] and chat.texts()[-1] == "Stopped."
    assert agent.approvals.pending_for(person(agent.accounts).address) is None


async def test_every_approval_is_audited(agent, llm, chat, registry, accounts):
    ran = []
    msg = await start_form_task(agent, llm, chat, registry, ran)
    await say(agent, "ok", button=msg.buttons[0].id)
    await settle(agent)
    lines = [json.loads(x) for x in accounts.vault.records(person(accounts).dir / "audit.jsonl")]
    assert lines[0]["asked"] == ["Felipe", "07700 900123"]
    assert lines[1]["decision"] == "approved" and lines[1]["final"] == ["Felipe", "07700 900123"]


async def test_unrelated_button_with_no_approval_open(agent, chat):
    await say(agent, "✅", button="ap-abcdef-yes")
    assert chat.texts()[-1] == "That approval isn't open any more."
    await asyncio.sleep(0)
