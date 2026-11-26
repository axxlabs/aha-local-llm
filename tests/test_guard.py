# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Every card goes through the guard, and the gate decides every action (SPEC §5.1, §5.2)."""

import re
from pathlib import Path
from types import SimpleNamespace

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

    async def ask(self, account, kind, title, fields, image=None, why=""):
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


async def test_opening_an_edited_address_is_checked_again():
    """The person may change the address on the card: what they approved goes through the rules again."""
    c = ctx(Decision(True, ["http://127.0.0.1/admin"]))
    _, action, refused = await guard.open_address(c, "https://example.com/?email=a@b.com", "fetch_url")
    assert c.approvals.asked and action.kind == "block"
    assert refused is not None and refused.text.startswith("Blocked by aha's safety policy")


async def test_a_declined_check_says_the_person_gave_up():
    c = ctx(Decision(False, note="later", reason="declined"))
    _, refused = await guard.ask(c, "check", "Show x.com you're human", [], declined="They gave up.")
    assert refused.text == "They gave up. They said: “later”. Follow that."
