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

EDIT_LINE = re.compile(r"^\s*(\d{1,2})\s*[:.)-]\s*(.*)$")
MAX_VALUE = 2000
MAX_LONG = 10_000  # longtext fields: an email's message
EDITABLE = ("text", "longtext", "secret", "taps")  # inputs on the card
TYPED = ("text", "longtext")  # also editable by the text fallback; a secret never goes through the chat
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
    reason: str = ""  # declined | expired | stopped | replan (the person wants a new plan, SPEC §3.4)
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
    editing: bool = False
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
    replan: str = ""  # a third button that sends the task back to its plan (the permissions card, SPEC §3.4)


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
        "asks first. You can change what it checks, how often and until when.",
    ),
    "permissions": Kind(
        "🔓",
        ("Allow", "Edit", "Ask each time"),
        "Before it starts, Aha! asks for what this request will need, all at once, instead of a card for each "
        "step. Untick what it shouldn't do without asking: that is asked when it happens. What you allow holds "
        "until the topic changes, and for a background task started from it. Every email or event it sends "
        "this way still shows in the chat. If this isn't what you had in mind, Change the plan: nothing is "
        "allowed, and Aha! asks what to change before doing anything.",
        open_inputs=True,
        replan="Change the plan",
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
        **({"replan": kind.replan} if kind.replan else {}),
    }
    if p.image is not None:
        out["image"] = p.image.to_dict()
    if why := p.why or kind.why:
        out["why"] = why
    return out


def apply_edit(fields: list[Field], text: str) -> list[Field] | None:
    """Text fallback. One field: the whole text. Several: lines of '<n>: <value>'. None if nothing matched."""
    text = text.strip()
    if not text:
        return None
    if len(fields) == 1:
        if fields[0].kind not in TYPED:
            return None
        return [Field(fields[0].label, text[:MAX_VALUE], fields[0].kind)]
    new = [Field(f.label, f.value, f.kind) for f in fields]
    matched = False
    for line in text.splitlines():
        m = EDIT_LINE.match(line)
        if not m:
            continue
        i = int(m.group(1)) - 1
        if 0 <= i < len(new) and new[i].kind in TYPED:
            new[i].value = m.group(2).strip()[: MAX_LONG if new[i].kind == "longtext" else MAX_VALUE]
            matched = True
    return new if matched else None


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
    def __init__(self, out: Out, expiry: float) -> None:
        self.out = out
        self.expiry = expiry
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
            # No expiry by default: the person answers when they check in (SPEC §5.3).
            decision = await asyncio.wait_for(asyncio.shield(p.future), timeout=self.expiry or None)
            state = "approved" if decision.approved else ("expired" if decision.reason == "expired" else "declined")
        except TimeoutError:
            decision = Decision(False, reason="expired")
            state = "expired"
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
        """A tap on a card: approve (with the card's possibly edited values), edit (text fallback) or decline."""
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
        elif decision == "replan" and KINDS.get(p.kind, KINDS["action"]).replan:
            self._resolve(p, Decision(False, reason="replan"))
        elif decision == "edit":
            if KINDS.get(p.kind, KINDS["action"]).open_inputs or not any(f.kind in TYPED for f in p.fields):
                await self.out.text(account, messages.EDIT_IN_CARD)
                return
            p.editing = True
            text_fields = [i for i, f in enumerate(p.fields, 1) if f.kind in TYPED]
            if len(p.fields) == 1:
                prompt = messages.EDIT_ONE
            else:
                i = text_fields[0]
                prompt = messages.EDIT_MANY.format(example=f"{i}: {p.fields[i - 1].value}")
            await self.out.text(account, prompt)

    async def reply_text(self, account: Account, text: str) -> bool:
        """A typed message while an approval is open. Returns False if there is none."""
        p = self._pending.get(account.address)
        if p is None:
            return False
        if p.editing:
            edited = apply_edit(p.fields, text)
            if edited is None:
                await self.out.text(account, messages.EDIT_UNCLEAR)
                return True
            await self.out.state(account, p.id, "replaced")
            p.fields, p.editing = edited, False
            p.id = secrets.token_hex(4)  # the old card no longer applies
            await self.out.card(account, card(p))
            return True
        # Free text while an approval is open: decline, and pass the text to the model as a note.
        self._resolve(p, Decision(False, note=text.strip(), reason="declined"))
        return True

    @staticmethod
    def _resolve(p: Pending, decision: Decision) -> None:
        if not p.future.done():
            p.future.set_result(decision)
