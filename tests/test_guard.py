# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Every card goes through the guard, and the gate decides every action (SPEC §5.1, §5.2)."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from aha_local.approvals import Decision, Field
from aha_local.tools import guard
from aha_local.tools.registry import TaskState

SRC = Path(__file__).resolve().parents[1] / "aha_local"


def test_only_the_guard_shows_cards():
    """No tool reaches the approvals itself: each card is decided, audited and declined in one place."""
    callers = [
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if re.search(r"approvals\.ask\(", p.read_text()) and p.name not in ("guard.py", "agent.py")
    ]
    assert callers == []


def test_sends_are_decided_by_the_gate():
    """An email, an event, a memory or a background task is a `send` for the gate, never a card a tool chose."""
    for name in ("mail.py", "calendar.py", "memory.py", "background.py"):
        text = (SRC / "tools" / name).read_text()
        assert "guard.send(" in text and "guard.ask(" not in text, name


class Approvals:
    def __init__(self, answer: Decision) -> None:
        self.answer, self.asked = answer, []

    async def ask(self, account, kind, title, fields, image=None, why="", always=False):
        self.asked.append((kind, title, [f.value for f in fields], why))
        return self.answer


def ctx(answer: Decision):
    task = TaskState()
    return SimpleNamespace(
        account=None, approvals=Approvals(answer), config=SimpleNamespace(allow_all_sites=True), task=task
    )


async def test_a_send_always_shows_a_card():
    c = ctx(Decision(True, ["x"]))
    decision, refused = await guard.send(c, "memory", "Remember this?", [Field("Fact", "x")])
    assert decision.approved and refused is None and c.approvals.asked[0][0] == "memory"


@pytest.mark.open_rule
async def test_opening_a_new_site_shows_its_address_and_runs_once_approved():
    c = ctx(Decision(True, ["https://example.com/"]))
    url, action, refused = await guard.open_address(c, "https://example.com/", "fetch_url")
    assert refused is None and action.kind == "open" and url == "https://example.com/"
    kind, title, values, _ = c.approvals.asked[0]
    assert (kind, title, values) == ("action", "Open example.com?", ["https://example.com/"])


async def test_a_declined_check_says_the_person_gave_up():
    c = ctx(Decision(False, note="later", reason="declined"))
    _, refused = await guard.ask(c, "check", "Show x.com you're human", [], declined="They gave up.")
    assert refused.text == "They gave up. They said: “later”. Follow that."


async def test_always_allow_covers_the_same_action_for_the_rest_of_the_request():
    """SPEC §5.3: every action card offers "Always allow for this task"; ticked, the same action (searching, typing
    on that site, an email to those people) runs without a card again, and nothing else does."""
    from aha_local.policy.gate import Action

    c = ctx(Decision(True, ["x"], always=True))
    _, refused = await guard.send(
        c, "email", "Send this email?", [Field("To", "sam@example.com")], to=["sam@example.com"]
    )
    assert refused is None and len(c.approvals.asked) == 1 and c.task.allowed == {"email:sam@example.com"}
    _, refused = await guard.send(
        c, "email", "Send this email?", [Field("To", "sam@example.com")], to=["sam@example.com"]
    )
    assert refused is None and len(c.approvals.asked) == 1  # the same: no card
    await guard.send(c, "email", "Send this email?", [Field("To", "ana@example.com")], to=["ana@example.com"])
    assert len(c.approvals.asked) == 2  # someone else: its own card
    assert guard.allow_key(Action("type", "waitrose.com")) == "type:waitrose.com"
    assert guard.allow_key(Action("send", "whatsapp.com")) == "message:whatsapp.com"
    assert guard.allow_label("message:whatsapp.com") == "Send messages on whatsapp.com"
    assert guard.allow_label("search") == "Search Google"
