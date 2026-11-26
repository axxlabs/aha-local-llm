# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The answer checks: chat formatting and numbers that no opened page supports."""

from aha_local import checks

from .conftest import say, settle


def test_markdown_stays_markdown_but_headings_become_bold():
    md = "## Opening hours\n**Saturday**: 10:00\n- item one\n\n\n\n[TAP](https://tap.pt)"
    assert checks.chat_format(md) == "**Opening hours**\n**Saturday**: 10:00\n- item one\n\n[TAP](https://tap.pt)"


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


async def test_numbers_read_on_googles_page_count_but_others_are_flagged(agent, llm, chat, registry, monkeypatch):
    """web_search is Google's own results page, read in the browser (SPEC §3.4): its answer box is a page read.
    A number no page showed is still flagged."""
    from aha_local.tools import web
    from aha_local.tools.registry import ToolResult

    async def search(args, ctx):
        return ToolResult(
            "Google's results for “british museum hours”:\nBritish Museum · Open ⋅ Closes 17:00", kind="page"
        )

    monkeypatch.setattr(web, "web_search", search)
    web.register(registry)

    llm.call("web_search", query="british museum hours")
    llm.answer("It closes at **17:00** today.")
    await say(agent, "when does the British Museum close?")
    await settle(agent)
    assert chat.texts()[-1] == "It closes at **17:00** today."

    llm.answer("Fridays it's open until 20:30.")
    llm.answer("Fridays it's open until 20:30.")  # after the check's retry, the model insists
    await say(agent, "and on Fridays?")
    await settle(agent)
    assert chat.texts()[-1].startswith("Fridays it's open until 20:30.\n\n⚠️ _Not confirmed on a page")


async def test_an_earlier_unconfirmed_answer_is_not_a_source(agent, llm, chat):
    llm.answer("It opens at 10:00.")
    llm.answer("It opens at 10:00.")
    await say(agent, "when does it open?")
    await settle(agent)
    assert "⚠️" in chat.texts()[-1]
    llm.answer("As I said, 10:00.")
    llm.answer("As I said, 10:00.")
    await say(agent, "sure?")
    await settle(agent)
    assert "⚠️" in chat.texts()[-1]


async def test_an_earlier_confirmed_answer_is_a_source(agent, llm, chat, registry):
    from aha_local.tools.registry import Tool, ToolResult, params

    async def page(args, ctx):
        return ToolResult("Booking confirmed: Fri 27 Nov, 14:00, ref FX12", kind="page")

    registry.add(Tool("browser_snapshot", "page", params(), page))
    llm.call("browser_snapshot")
    llm.answer("Booked for 14:00.")
    await say(agent, "book it")
    await settle(agent)
    assert chat.texts()[-1] == "Booked for 14:00."
    llm.answer("Your table is at 14:00.")
    await say(agent, "what time was it?")
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
    await say(agent, "when does it open?")
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


def test_a_meeting_that_is_scheduled_is_not_a_schedule_claim():
    """Seen: "your meeting is scheduled for 14:00" was sent back as an unbacked schedule claim, and the model took
    back a meeting it had found. Only Aha! saying it scheduled something itself counts."""
    from aha_local.checks import unbacked_schedule_claim

    for calendar_answer in (
        "Your meeting with Roderick is scheduled for 26/11/2026 at 13:00.",
        "You have a 1:1 scheduled on 25/11/2026.",
        "The call was rescheduled to 02/12/2026.",
    ):
        assert not unbacked_schedule_claim(calendar_answer, {"calendar_list"}), calendar_answer
    for claim in (
        "I've scheduled a reminder for 18:00.",
        "Done, I scheduled it for every weekday at 8.",
        "Your reminder is set for tomorrow.",
        "I'll remind you at 6.",
        "I'll check the price every morning.",
        "Scheduled. Every weekday at 08:00 I'll check the forecast.",
        "Done! Scheduled it for 18:00.",
    ):
        assert unbacked_schedule_claim(claim, set()), claim
