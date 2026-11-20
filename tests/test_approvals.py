# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""SPEC §5.3, §11.2: approve, edit, decline, free-text decline, stop, expiry; what runs is what was approved."""

import asyncio
import json

from aha_local.approvals import Field, apply_edit
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
            return ToolResult(f"declined; note={d.note}", error=True)
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


async def test_edit_numbered_fields_then_approve(agent, llm, chat, registry):
    ran = []
    msg = await start_form_task(agent, llm, chat, registry, ran)
    await say(agent, "✏️ Edit", button=msg.buttons[1].id)
    prompt = await chat.next()
    assert "1: Felipe" in prompt.text
    await say(agent, "2: 07700 900456")
    again = await chat.next()
    assert "Phone: 07700 900456" in again.text
    await say(agent, "✅ Approve", button=msg.buttons[0].id)  # the old buttons no longer apply
    assert (await chat.next()).text == "That approval isn't open any more."
    await say(agent, "✅ Approve", button=again.buttons[0].id)
    await settle(agent)
    assert ran[0]["fields"][1]["value"] == "07700 900456"
    assert ran[0]["fields"][0]["value"] == "Felipe"


async def test_free_text_declines_with_a_note(agent, llm, chat, registry):
    ran = []
    await start_form_task(agent, llm, chat, registry, ran)
    await say(agent, "no, use my work phone")
    await settle(agent)
    assert ran == []
    tool_msg = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]
    assert "note=no, use my work phone" in tool_msg["content"]


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


async def test_expiry_declines(agent, llm, chat, registry):
    agent.approvals.expiry = 0.05
    ran = []
    await start_form_task(agent, llm, chat, registry, ran)
    await settle(agent)
    assert ran == [] and chat.texts()[-1] == "I stopped waiting for your approval, so I didn't do it."


async def test_every_approval_is_audited(agent, llm, chat, registry, accounts):
    ran = []
    msg = await start_form_task(agent, llm, chat, registry, ran)
    await say(agent, "ok", button=msg.buttons[0].id)
    await settle(agent)
    lines = [json.loads(x) for x in (person(accounts).dir / "audit.jsonl").read_text().splitlines()]
    assert lines[0]["asked"] == ["Felipe", "07700 900123"]
    assert lines[1]["decision"] == "approved" and lines[1]["final"] == ["Felipe", "07700 900123"]


def test_apply_edit():
    one = [Field("Text", "a")]
    assert apply_edit(one, "  new text ")[0].value == "new text"
    two = [Field("A", "1"), Field("B", "2")]
    assert [f.value for f in apply_edit(two, "2: x\n1) y")] == ["y", "x"]
    assert apply_edit(two, "just words") is None
    assert apply_edit(two, "9: out of range") is None


async def test_unrelated_button_with_no_approval_open(agent, chat):
    await say(agent, "✅", button="ap-abcdef-yes")
    assert chat.texts()[-1] == "That approval isn't open any more."
    await asyncio.sleep(0)
