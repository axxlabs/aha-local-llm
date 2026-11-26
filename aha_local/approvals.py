# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Approvals (SPEC §5.1, §5.3).

A tool call that needs approval becomes an approval card in the app. The task waits on a future that only the
person's reply can resolve: the model has no tool, message or argument that reaches this module. Declines,
expiry, `stop` and errors never run the action (default deny), and what the person approved, edits included,
is exactly what runs.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass, field
from typing import Protocol

from . import messages
from .accounts import Account
from .items import Image

MAX_VALUE = 2000
MAX_LONG = 10_000  # longtext fields: an email's message
EDITABLE = ("text", "longtext", "secret", "taps")  # inputs on the card
MASK = "••••"
TAPS = re.compile(r"(?:\d{1,5},\d{1,5}(?:;\d{1,5},\d{1,5}){0,19})?")  # a check card's taps: "x,y;x,y", or none


@dataclass
class Field:
    label: str
    value: str
    # text | longtext | bool | info (shown, not editable) | secret (typed by the person, masked) |
    # taps (points the person tapped on a check card's screenshot)
    kind: str = "text"


@dataclass
class Decision:
    approved: bool
    values: list[str] = field(default_factory=list)
    note: str = ""
    reason: str = ""  # declined | stopped | message (the person wrote instead)
    card: str = ""  # the card it answered (a background task keeps it: the topic that started it, SPEC §7.4)


@dataclass
class Pending:
    id: str
    account: Account
    kind: str
    title: str
    fields: list[Field]
    image: Image | None
    future: asyncio.Future
    why: str = ""  # why this card is shown (the (i)); "" → the kind's own reason (KINDS)


class Out(Protocol):
    """Where approvals are shown (the channel to the person's devices)."""

    async def card(self, account: Account, card: dict) -> None: ...
    async def state(self, account: Account, approval_id: str, state: str) -> None: ...
    async def text(self, account: Account, text: str) -> None: ...


@dataclass(frozen=True)
class Kind:
    """One kind of card, all it needs in one place: its icon, its buttons (yes, edit, no), why it's shown when the
    caller doesn't say more (the (i) on the card, SPEC §7.4), and whether its inputs are open from the start (the
    person types or taps). Cards of the browser and of opening addresses take their reason from the gate's rule
    instead (policy.gate.WHY)."""

    icon: str
    buttons: tuple[str, str, str]
    why: str = ""
    open_inputs: bool = False


KINDS = {
    "action": Kind("✋", ("Approve", "Edit", "Decline")),
    "email": Kind(
        "✉️",
        ("Send", "Edit", "Don't send"),
        "Every email asks first, always: nothing is sent in your name without your OK. It's signed as written by "
        "Aha!, and you can change any of it before it goes.",
    ),
    "draft": Kind(
        "✉️",
        ("Save draft", "Edit", "Don't save"),
        "A draft is saved into your mailbox, so it's shown to you first. It isn't sent.",
    ),
    "calendar": Kind(
        "📅",
        ("Save", "Edit", "Don't"),
        "Adding or changing a calendar event always asks first: invitations can go out to the people in it, and "
        "your calendar is yours. You can change it before it's saved.",
    ),
    "calendar_delete": Kind(
        "📅",
        ("Delete it", "Edit", "Keep it"),
        "Deleting a calendar event always asks first: it can't be undone, and the people invited may be told.",
    ),
    "task": Kind(
        "⏳",
        ("Start", "Edit", "Don't"),
        "A background task checks on its own later, while you're not using Aha!, without you there, so it always "
        "asks first. You can change what it checks, how often and what ends it.",
    ),
    "memory": Kind(
        "🧠",
        ("Save", "Edit", "Don't"),
        "Aha! keeps something in its memory for future chats only with your OK, so you always see what it remembers.",
    ),
    "forget": Kind("🧠", ("Forget it", "Edit", "Keep it"), "Forgetting a fact can't be undone, so it asks first."),
    "signin": Kind(
        "🔑",
        ("Sign in", "Edit", "Don't"),
        "A site wants you to sign in. What you type here goes straight into the site's sign-in form: the model "
        "never sees it, and it isn't kept in the chat. Tick save and Aha! signs you in on this site by itself next "
        "time.",
        open_inputs=True,
    ),
    "check": Kind(
        "🧩",
        ("Send taps", "Edit", "Give up"),
        "The site wants to check that a person is there (a puzzle or a tap). Only you can answer it: Aha! taps "
        "where you tap.",
        open_inputs=True,
    ),
}


def masked(fields: list[Field], values: list[str]) -> list[str]:
    """Values as they may be written down (audit): secrets never are."""
    return [MASK if f.kind == "secret" and v else v for f, v in zip(fields, values, strict=False)]


def card(p: Pending) -> dict:
    kind = KINDS.get(p.kind, KINDS["action"])
    yes, edit, no = kind.buttons
    editable = any(f.kind in EDITABLE for f in p.fields)
    out = {
        "t": "approval",
        "id": p.id,
        "kind": p.kind,
        "icon": kind.icon,
        "title": p.title,
        "fields": [
            {
                "label": f.label,
                "value": "" if f.kind == "secret" else f.value,
                "editable": f.kind in EDITABLE,
                "kind": f.kind,
            }
            for f in p.fields
        ],
        "buttons": [yes, edit, no] if editable and not kind.open_inputs else [yes, no],
    }
    if p.image is not None:
        out["image"] = p.image.to_dict()
    if why := p.why or kind.why:
        out["why"] = why
    return out


def checked_values(fields: list[Field], values: list) -> list[str] | None:
    """Values edited in the card: same count; only text fields may change; booleans stay booleans."""
    if not isinstance(values, list) or len(values) != len(fields):
        return None
    out = []
    for f, v in zip(fields, values, strict=True):
        if not isinstance(v, str) or len(v) > (MAX_LONG if f.kind == "longtext" else MAX_VALUE):
            return None
        if f.kind == "taps":
            if not TAPS.fullmatch(v):
                return None
            out.append(v)
        elif f.kind in EDITABLE:
            out.append(v)
        elif f.kind == "bool" and v.lower() in ("true", "false"):
            out.append(v.lower())
        elif v == f.value:
            out.append(v)
        else:
            return None
    return out


class Approvals:
    def __init__(self, out: Out) -> None:
        self.out = out
        self._pending: dict[str, Pending] = {}  # by account id

    def pending_for(self, address: str) -> Pending | None:
        return self._pending.get(address)

    async def ask(
        self,
        account: Account,
        kind: str,
        title: str,
        fields: list[Field],
        image: Image | None = None,
        why: str = "",
    ) -> Decision:
        p = Pending(
            secrets.token_hex(4), account, kind, title, fields, image, asyncio.get_running_loop().create_future()
        )
        p.why = why
        self._pending[account.address] = p
        account.audit(
            {"approval": p.id, "kind": kind, "title": title, "asked": masked(fields, [f.value for f in fields])}
        )
        state = "stopped"
        try:
            await self.out.card(account, card(p))
            # A card waits until it's answered: the person answers when they check in (SPEC §5.3).
            decision = await asyncio.shield(p.future)
            state = "approved" if decision.approved else "declined"
        except asyncio.CancelledError:
            account.audit({"approval": p.id, "decision": "stopped"})
            raise
        finally:
            if self._pending.get(account.address) is p:
                del self._pending[account.address]
            await asyncio.shield(self.out.state(account, p.id, state))
        decision.card = p.id
        account.audit(
            {
                "approval": p.id,
                "decision": "approved" if decision.approved else decision.reason,
                "final": masked(fields, decision.values),
                "note": decision.note,
            }
        )
        return decision

    async def answer(
        self, account: Account, approval_id: str, decision: str, values: list | None = None, note: str = ""
    ) -> None:
        """A tap on a card: approve (with the values as edited in the card) or decline."""
        p = self._pending.get(account.address)
        if p is None or approval_id != p.id:
            await self.out.text(account, messages.APPROVAL_STALE)
            return
        if decision == "approve":
            final = [f.value for f in p.fields] if values is None else checked_values(p.fields, values)
            if final is None:
                await self.out.text(account, messages.EDIT_UNCLEAR)
                return
            self._resolve(p, Decision(True, final))
        elif decision == "decline":
            self._resolve(p, Decision(False, note=str(note)[:MAX_VALUE].strip(), reason="declined"))

    def cancel_for_message(self, account: Account) -> None:
        """A message while a card is open cancels it, not allowed; the message goes on to the request like any
        other, so the model reads it right after the call it was waiting on (SPEC §5.3)."""
        if (p := self._pending.get(account.address)) is not None:
            self._resolve(p, Decision(False, reason="message"))

    @staticmethod
    def _resolve(p: Pending, decision: Decision) -> None:
        if not p.future.done():
            p.future.set_result(decision)
