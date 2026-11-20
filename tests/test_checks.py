# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The answer checks: chat formatting and numbers that no opened page supports."""

from aha_local import checks

from .conftest import said, settle


def test_markdown_becomes_whatsapp_formatting():
    md = "## Opening hours\n**Saturday**: 10:00\n- item one\n* item two\n[TAP](https://tap.pt) and [https://x.io](https://x.io)"
    out = checks.chat_format(md)
    assert out == "*Opening hours*\n*Saturday*: 10:00\n• item one\n• item two\nTAP: https://tap.pt and https://x.io"


def test_numbers_found_and_matched_loosely():
    answer = "Opens at 10am, closes 17:00. Fare £212 return. Call 020 7323 8000."
    assert checks.numbers_in(answer) == ["17:00", "10am", "£212", "020 7323 8000"]
    page = "Open daily 10.00–17.00. Return from £ 212. Phone: 020-7323-8000"
    assert checks.unverified(answer, [page]) == []
    assert checks.unverified(answer, ["Open daily 09:00"]) == ["17:00", "10am", "£212", "020 7323 8000"]


def test_twelve_and_twenty_four_hour_times_match():
    assert checks.unverified("from 5pm", ["Opens 17:00"]) == []
    assert checks.unverified("at 17:30", ["5:30pm"]) == []


def test_plain_counts_and_years_are_not_checked():
    assert checks.numbers_in("2 people on 27 Nov 2026, 3 options") == []


async def test_snippet_numbers_are_flagged_but_page_numbers_are_not(agent, llm, chat, registry, monkeypatch):
    from aha_local.tools import web
    from aha_local.tools.registry import ToolResult

    async def search(args, ctx):
        return ToolResult("1. Visit | British Museum (snippet: open 10:00–17:00)")

    async def fetch(args, ctx):
        return ToolResult("British Museum. Open daily 10:00–17:00, Fridays until 20:30.", kind="page")

    monkeypatch.setattr(web, "web_search", search)
    monkeypatch.setattr(web, "fetch_url", fetch)
    web.register(registry)

    llm.call("web_search", query="british museum hours")
    llm.answer("It opens at **10:00** and closes at 17:00.")
    llm.answer("It opens at **10:00** and closes at 17:00.")  # after the check's retry, the model insists
    await agent.inbound(said("when does the British Museum open?"))
    await settle(agent)
    assert chat.texts()[-1].startswith("It opens at *10:00* and closes at 17:00.\n\n⚠️ _Not confirmed on a page")

    llm.call("fetch_url", url="https://britishmuseum.org/visit")
    llm.answer("Fridays it's open until 20:30.")
    await agent.inbound(said("and on Fridays?"))
    await settle(agent)
    assert chat.texts()[-1] == "Fridays it's open until 20:30."


async def test_an_earlier_unconfirmed_answer_is_not_a_source(agent, llm, chat):
    llm.answer("It opens at 10:00.")
    llm.answer("It opens at 10:00.")
    await agent.inbound(said("when does it open?"))
    await settle(agent)
    assert "⚠️" in chat.texts()[-1]
    llm.answer("As I said, 10:00.")
    llm.answer("As I said, 10:00.")
    await agent.inbound(said("sure?"))
    await settle(agent)
    assert "⚠️" in chat.texts()[-1]


async def test_an_earlier_confirmed_answer_is_a_source(agent, llm, chat, registry):
    from aha_local.tools.registry import Tool, ToolResult, params

    async def page(args, ctx):
        return ToolResult("Booking confirmed: Fri 27 Nov, 14:00, ref FX12", kind="page")

    registry.add(Tool("browser_snapshot", "page", params(), page))
    llm.call("browser_snapshot")
    llm.answer("Booked for 14:00.")
    await agent.inbound(said("book it"))
    await settle(agent)
    assert chat.texts()[-1] == "Booked for 14:00."
    llm.answer("Your table is at 14:00.")
    await agent.inbound(said("what time was it?"))
    await settle(agent)
    assert chat.texts()[-1] == "Your table is at 14:00."


async def test_the_check_gives_the_model_one_chance_to_confirm(agent, llm, chat, registry):
    from aha_local.tools.registry import Tool, ToolResult, params

    async def fetch(args, ctx):
        return ToolResult("Open daily 10:00 to 17:00", kind="page")

    registry.add(Tool("fetch_url", "read", params(), fetch))
    llm.answer("It opens at 10:00.")  # from memory: not confirmed
    llm.call("fetch_url", url="https://britishmuseum.org/visit")  # the retry makes it check
    llm.answer("It opens at 10:00 (britishmuseum.org).")
    await agent.inbound(said("when does it open?"))
    await settle(agent)
    assert chat.texts() == ["It opens at 10:00 (britishmuseum.org)."]
    retry = [m for m in llm.requests[1]["messages"] if m["role"] == "user"][-1]["content"]
    assert retry.startswith("[aha check]") and "10:00" in retry


def test_one_digit_times_are_checked():
    assert checks.numbers_in("closes at 5 pm, or 5pm") == ["5 pm"]


def test_memory_claims_need_a_memory_tool():
    from aha_local.checks import unbacked_memory_claim

    assert unbacked_memory_claim("forget where I live", "Done, I've forgotten where you live.", set())
    assert not unbacked_memory_claim("forget where I live", "Done, I've forgotten it.", {"memory_delete"})
    assert not unbacked_memory_claim("compare razors", "I removed the filter and found two.", set())
    assert not unbacked_memory_claim("forget it", "Sure, which one should I drop?", set())
    assert not unbacked_memory_claim("forget where I live", "I never saved where you live, so there's nothing.", set())
    assert not unbacked_memory_claim("forget where I live", "I haven't saved that, so nothing to forget.", set())
    assert unbacked_memory_claim("remember my dentist", "Done, I've saved it.", set())
    assert unbacked_memory_claim("forget that", "I'll no longer remember it.", set())
