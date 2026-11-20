"""Approvals in the chat (SPEC §5.1, §5.3).

A tool call that needs approval becomes a chat message with three buttons. The task waits on a future that
only the person's reply can resolve: the model has no tool, message or argument that reaches this module.
Declines, expiry, `stop` and errors never run the action (default deny), and what the person approved,
edits included, is exactly what runs.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import messages
from .accounts import Account
from .protocol import Button, Image

BUTTON_PATTERN = re.compile(r"^ap-([a-z0-9]{6})-(yes|edit|no)$")
EDIT_LINE = re.compile(r"^\s*(\d{1,2})\s*[:.)-]\s*(.*)$")


@dataclass
class Field:
    label: str
    value: str
    kind: str = "text"  # text | bool


@dataclass
class Decision:
    approved: bool
    values: list[str] = field(default_factory=list)
    note: str = ""
    reason: str = ""  # declined | expired | stopped


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


Send = Callable[[Account, str, list[Button], Image | None], Awaitable[None]]


def render(p: Pending) -> tuple[str, list[Button]]:
    icon = "🧠" if p.kind in ("memory", "forget") else "✋"
    lines = [f"{icon} *{p.title}*"]
    if len(p.fields) == 1:
        f = p.fields[0]
        lines.append(f"_{f.value}_" if p.kind in ("memory", "forget") else f"{f.label}: {f.value}")
    else:
        lines += [f"{i}. {f.label}: {f.value}" for i, f in enumerate(p.fields, 1)]
    yes, edit, no = messages.APPROVE_LABELS.get(p.kind, messages.APPROVE_LABELS["action"])
    buttons = [Button(id=f"ap-{p.id}-yes", label=yes), Button(id=f"ap-{p.id}-no", label=no)]
    if any(f.kind == "text" for f in p.fields):
        buttons.insert(1, Button(id=f"ap-{p.id}-edit", label=edit))
    return "\n".join(lines), buttons


def apply_edit(fields: list[Field], text: str) -> list[Field] | None:
    """One field: the whole text. Several: lines of '<n>: <value>'. None if nothing matched."""
    text = text.strip()
    if not text:
        return None
    if len(fields) == 1:
        return [Field(fields[0].label, text, fields[0].kind)]
    new = [Field(f.label, f.value, f.kind) for f in fields]
    matched = False
    for line in text.splitlines():
        m = EDIT_LINE.match(line)
        if not m:
            continue
        i = int(m.group(1)) - 1
        if 0 <= i < len(new) and new[i].kind == "text":
            new[i].value = m.group(2).strip()
            matched = True
    return new if matched else None


class Approvals:
    def __init__(self, send: Send, expiry: float) -> None:
        self.send = send
        self.expiry = expiry
        self._pending: dict[str, Pending] = {}  # by account address

    def pending_for(self, address: str) -> Pending | None:
        return self._pending.get(address)

    async def ask(
        self, account: Account, kind: str, title: str, fields: list[Field], image: Image | None = None
    ) -> Decision:
        p = Pending(
            secrets.token_hex(3), account, kind, title, fields, image, asyncio.get_running_loop().create_future()
        )
        self._pending[account.address] = p
        text, buttons = render(p)
        account.audit({"approval": p.id, "kind": kind, "title": title, "asked": [f.value for f in fields]})
        try:
            await self.send(account, text, buttons, image)
            decision = await asyncio.wait_for(asyncio.shield(p.future), timeout=self.expiry)
        except TimeoutError:
            decision = Decision(False, reason="expired")
        except asyncio.CancelledError:
            account.audit({"approval": p.id, "decision": "stopped"})
            raise
        finally:
            if self._pending.get(account.address) is p:
                del self._pending[account.address]
        account.audit(
            {
                "approval": p.id,
                "decision": "approved" if decision.approved else decision.reason,
                "final": decision.values,
                "note": decision.note,
            }
        )
        return decision

    async def reply(self, account: Account, text: str, button: str | None) -> bool:
        """Route a person's message to their open approval. Returns False if they have none."""
        p = self._pending.get(account.address)
        if button is not None:
            m = BUTTON_PATTERN.match(button)
            if m is None:
                return False
            if p is None or m.group(1) != p.id:
                await self.send(account, messages.APPROVAL_STALE, [], None)
                return True
            action = m.group(2)
            if action == "yes":
                self._resolve(p, Decision(True, [f.value for f in p.fields]))
            elif action == "no":
                self._resolve(p, Decision(False, reason="declined"))
            else:
                p.editing = True
                text_fields = [i for i, f in enumerate(p.fields, 1) if f.kind == "text"]
                if len(p.fields) == 1:
                    prompt = messages.EDIT_ONE
                else:
                    i = text_fields[0]
                    prompt = messages.EDIT_MANY.format(example=f"{i}: {p.fields[i - 1].value}")
                await self.send(account, prompt, [], None)
            return True
        if p is None:
            return False
        if p.editing:
            edited = apply_edit(p.fields, text)
            if edited is None:
                await self.send(account, messages.EDIT_UNCLEAR, [], None)
                return True
            p.fields, p.editing = edited, False
            p.id = secrets.token_hex(3)  # buttons of the old version no longer apply
            body, buttons = render(p)
            await self.send(account, body, buttons, None)
            return True
        # Free text while an approval is open: decline, and pass the text to the model as a note.
        self._resolve(p, Decision(False, note=text.strip(), reason="declined"))
        return True

    @staticmethod
    def _resolve(p: Pending, decision: Decision) -> None:
        if not p.future.done():
            p.future.set_result(decision)
