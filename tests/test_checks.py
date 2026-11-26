# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Checks on the final answer (SPEC §3.5.1): from the task's state, never from its words. The person audits the
text; the harness doesn't second-guess it with patterns."""

from aha_local import checks

from .conftest import say, settle


async def test_the_answers_words_are_never_checked(agent, llm, chat):
    """No claim, number or weekday in the text sends a draft back or gets a warning."""
    answer = "Done: I've saved that and booked it. It opens at **10:00** on Saturday 29/11/2026, £42."
    llm.answer(answer)
    await say(agent, "remember this and book it")
    await settle(agent)
    assert chat.texts() == [answer] and len(llm.requests) == 1


def test_the_checks_are_the_pages_state():
    assert [c.name for c in checks.CLAIM_CHECKS] == ["challenge", "sign_in_page"]
