# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The end-to-end channel between this computer and the people's devices (SPEC §6.4–§6.6).

- Pairing: answers a device that joined with a rendezvous code, checks its proof of the QR secret, adds it to
  the account and gets it a binding certificate (routing only).
- Messages: decrypts what devices send (dropping anything that doesn't decrypt), hands people's messages to the
  agent, and sends the agent's output to every device of the account, numbered, written to the transcript.
- Devices that aren't connected get a push whose content is sealed with the device's push key.

Only devices on this computer's own list are ever trusted; certificates from the gateway never are (§6.4).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__, actions, messages, reports
from ..accounts import Account, Accounts, Device
from ..certs import needs_renewal
from ..items import Image
from ..protocol import E2E, Cert, CertIssued, CertRequest, DeviceStatus, Pair, Push, Report, ReportResult, Revoke, b64
from ..tools import background, google
from . import crypto, webpush

log = logging.getLogger("aha.channel")

PAIR_LIFETIME = 600
MAX_TEXT = 20_000
SYNC_LIMIT = 500
HISTORY_LIMIT = 50
REPLY_BYTES = 1_500_000  # items in one sync or history reply, as JSON: well inside a 2 MB ratchet message


TRACE_KINDS = ("user", "model", "tool", "check", "item", "stopped", "answer")


def trace(records: list[dict], seq: int, budget: int = REPLY_BYTES) -> list[dict]:
    """The records of the task that sent item `seq`, oldest first, shortened to fit one reply. Empty when the
    item isn't from a task, or from before tasks were tagged."""
    task = next(
        (r.get("task") for r in records if r.get("kind") == "item" and (r.get("item") or {}).get("seq") == seq),
        None,
    )
    if not task:
        return []
    events = []
    for r in records:
        if r.get("task") != task or r.get("kind") not in TRACE_KINDS:
            continue
        e = {k: v for k, v in r.items() if k != "task"}
        if e["kind"] == "item":
            it = e.pop("item") or {}
            e.update({"t": it.get("t"), "seq": it.get("seq"), "text": it.get("text") or it.get("title") or ""})
        events.append(e)
    cap = 20_000
    while len(json.dumps(events, ensure_ascii=False)) > budget and cap > 200:
        cap //= 2
        events = [{k: _cut(v, cap) for k, v in e.items()} for e in events]
    return events


def _cut(value, cap: int):
    if isinstance(value, str) and len(value) > cap:
        return value[:cap] + "…"
    if isinstance(value, list):
        return [_cut(v, cap) for v in value]
    if isinstance(value, dict):
        return {k: _cut(v, cap) for k, v in value.items()}
    return value


def fit(items: list[dict], budget: int = REPLY_BYTES) -> list[dict]:
    """Items that fit one reply (SPEC §6.6): leave out the oldest items' pictures first (a card's screenshot is
    a few hundred KB), then, only if still too large, the oldest items themselves."""
    sizes = [len(json.dumps(it, ensure_ascii=False)) for it in items]
    total = sum(sizes)
    out = list(items)
    for i, it in enumerate(out):
        if total <= budget:
            return out
        if "image" in it:
            slim = {k: v for k, v in it.items() if k != "image"}
            total -= sizes[i] - len(json.dumps(slim, ensure_ascii=False))
            out[i] = slim
    while out and total > budget:
        total -= len(json.dumps(out.pop(0), ensure_ascii=False))
    return out


HISTORY_DAYS = (None, 365, 90, 30, 7, 1)  # how long the computer may keep a person's history (§3.8.1)
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SendFrame = Callable[[Any], Awaitable[None]]


MAX_PHOTO_BYTES = 1_000_000  # the app sends ≤1600 px JPEGs, a few hundred kB (SPEC §6.5)
PHOTO_MAGIC = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",), "image/webp": (b"RIFF",)}


def photo(value: object) -> Image | None:
    """A photo from the person: a known type whose bytes start like that type, within the size limit."""
    if not isinstance(value, dict) or value.get("mime") not in PHOTO_MAGIC or not isinstance(value.get("b64"), str):
        return None
    b64 = value["b64"]
    if len(b64) > MAX_PHOTO_BYTES * 4 // 3 + 4:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw.startswith(PHOTO_MAGIC[value["mime"]]) or (value["mime"] == "image/webp" and raw[8:12] != b"WEBP"):
        return None
    return Image(mime=value["mime"], b64=b64)


def clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return CONTROL.sub("", unicodedata.normalize("NFC", value))[:limit]


def load_or_create_identity(vault, path: Path) -> crypto.ComputerIdentity:
    """The computer's end-to-end identity, encrypted at rest (SPEC §3.8.1)."""
    raw = vault.read(path)
    if raw is not None:
        return crypto.ComputerIdentity.from_dict(json.loads(raw))
    identity = crypto.ComputerIdentity.generate()
    vault.create(path, json.dumps(identity.to_dict()).encode())
    return identity


@dataclass
class Request:
    """A message from one of the person's devices, and how to answer that device."""

    account: Account
    device: Device
    msg: dict
    reply: Callable[[dict], Awaitable[bool]]


@dataclass
class PairingState:
    account_id: str
    offer: crypto.PairingOffer
    expires: float
    done: asyncio.Future


class Channel:
    def __init__(
        self,
        accounts: Accounts,
        keys_dir: Path,
        send_frame: SendFrame,
        app_url: str,
        device_url: str,
        household: str,
        history_days: int = 30,
        push_preview: bool = False,
        models=None,
    ) -> None:
        self.accounts = accounts
        self.identity = load_or_create_identity(accounts.vault, keys_dir / "e2e.json")
        self.send_frame = send_frame
        self.app_url = app_url.rstrip("/")
        self.device_url = device_url
        self.household = household
        self.history_days = history_days
        self.push_preview = push_preview
        self.agent = None  # set by the service once the agent exists (it needs this channel as its output)
        self.online: set[str] = set()
        self.pairings: dict[str, PairingState] = {}
        self._cert_waiters: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self.gateway_pin = ""
        self.gateway_features: frozenset[str] = frozenset()  # from the gateway's hello (SPEC §6.5)
        self.models = models  # the household's model choice (models.ModelChoice), for Settings
        self.drafts: dict[str, reports.Draft] = {}  # by account id: the report the person is looking at (§7.7)
        self._report_waiters: dict[str, asyncio.Future] = {}

    # ---- output for the agent (agent.AgentOut, approvals.Out)

    async def message(self, account: Account, text: str, image: Image | None = None, open: str = "") -> None:
        item: dict = {"t": "msg", "role": "aha", "text": text}
        if image is not None:
            item["image"] = image.to_dict()
        if open:  # "asks" or "stopped": the app keeps it unfolded after a break (SPEC §7.4 "Folding")
            item["open"] = open
        await self.publish(account, item, notify=True)

    async def title(self, account: Account, start: int, text: str, new: bool = False) -> None:
        """The title of the conversation since item `start`, shown when the app folds it (SPEC §7.4); `new`: a new
        subject starts there, so the part before it folds now."""
        await self.publish(account, {"t": "title", "from": start, "text": text, **({"new": True} if new else {})})

    async def tool(self, account: Account, label: str) -> None:
        """A tool the model starts using, as it happens (SPEC §7.4 "Tools used"): a chat item, no push."""
        await self.publish(account, {"t": "tool", "label": label})

    async def text(self, account: Account, text: str) -> None:
        await self.message(account, text)

    async def card(self, account: Account, card: dict) -> None:
        await self.publish(account, card, notify=True)

    async def state(self, account: Account, approval_id: str, state: str) -> None:
        await self.publish(account, {"t": "approval_state", "id": approval_id, "state": state})

    async def typing(self, account: Account, on: bool, queued: bool = False) -> None:
        """ "Aha! is working…", or, while it waits for another person's request to finish, that it's queued."""
        await self.to_devices(account, {"t": "typing", "on": on, **({"queued": True} if queued else {})})

    async def publish(self, account: Account, item: dict, notify: bool = False) -> dict:
        """Number the item, keep it in the transcript, send it to every device of the account."""
        item = {**item, "seq": account.next_seq(), "ts": int(time.time())}
        account.transcript({"kind": "item", "item": item})
        live = {**item, "item": True}  # a chat item, whatever its type: the app keeps and draws it (SPEC §6.5)
        for device in list(account.devices.values()):
            if self.is_online(device):
                await self._send(account, device, live)
            elif notify:
                await self._push(account, device, item)
        return item

    def is_online(self, device: Device) -> bool:
        return device.dev in self.online

    async def to_devices(self, account: Account, message: dict) -> None:
        """To every connected device of the account, outside the transcript (settings, not chat)."""
        for device in list(account.devices.values()):
            if self.is_online(device):
                await self._send(account, device, message)

    async def history_changed(self, account: Account) -> None:
        """History before `start` is gone (its length, or Clear history): connected devices delete it too."""
        await self.to_devices(account, {"t": "history_start", "start": account.history_start()})

    async def _send(self, account: Account, device: Device, message: dict) -> bool:
        try:
            ratchet = crypto.Ratchet.from_dict(device.ratchet)
            box = crypto.encrypt_json(ratchet, message)
        except (crypto.CryptoError, KeyError, TypeError, ValueError):
            return False  # the device hasn't spoken yet, so there's no sending chain; it will sync
        device.ratchet = ratchet.to_dict()
        account.save_device(device)  # saved before sending (SPEC §6.8)
        try:
            await self.send_frame(E2E(dev=device.dev, box=box))
        except ConnectionError:
            self.online.discard(device.dev)
            return False
        return True

    async def _push(self, account: Account, device: Device, item: dict) -> None:
        if not device.push or not webpush.valid_subscription(device.push):
            return
        payload: dict = {"seq": item["seq"]}
        if self.push_preview and item.get("t") == "msg":
            payload["preview"] = (item.get("text") or "").splitlines()[0][:120] if item.get("text") else ""
        sealed = crypto.push_seal(crypto.b64d(device.push_key, 32), payload)
        body = webpush.encrypt(device.push, json.dumps(sealed).encode())
        urgency = "high" if item.get("t") == "approval" else "normal"
        with contextlib.suppress(ConnectionError, ValueError):
            await self.send_frame(Push(endpoint=device.push["endpoint"], body=b64(body), ttl=86_400, urgency=urgency))

    # ---- frames from the gateway

    async def on_e2e(self, frame: E2E) -> None:
        found = self.accounts.by_device(frame.dev)
        if found is None:
            log.info("dropped a message from a device not on this computer's list")
            return
        account, device = found
        try:
            ratchet = crypto.Ratchet.from_dict(device.ratchet)
            message = crypto.decrypt_json(ratchet, frame.box)
        except (crypto.CryptoError, KeyError, TypeError, ValueError):
            log.info("dropped a message that didn't decrypt")
            return
        device.ratchet = ratchet.to_dict()
        device.last_seen = int(time.time())
        account.save_device(device)  # saved after accepting it
        self.online.add(device.dev)
        try:
            await self._dispatch(account, device, message)
        except Exception:
            log.exception("handling a device message failed")

    async def on_device_status(self, frame: DeviceStatus) -> None:
        if frame.online:
            self.online.add(frame.dev)
            found = self.accounts.by_device(frame.dev)
            if found and found[1].certificate and needs_renewal(found[1].certificate):
                task = asyncio.create_task(self._renew(*found))  # waits for a frame from this same connection
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        else:
            self.online.discard(frame.dev)

    def on_cert(self, frame: CertIssued) -> None:
        waiter = self._cert_waiters.pop(frame.id, None)
        if waiter and not waiter.done():
            waiter.set_result(frame.cert)

    def on_disconnect(self) -> None:
        self.online.clear()

    # ---- what devices ask for

    async def _dispatch(self, account: Account, device: Device, msg: dict) -> None:
        handler = HANDLERS.get(msg.get("t"))  # type: ignore[arg-type]
        req = Request(account, device, msg, lambda m: self._send(account, device, m))
        if handler is None:
            await req.reply({"t": "error", "code": "unknown_type"})
            return
        await handler(self, req)

    async def _hello(self, r: Request) -> None:
        first = r.device.name == "New device"
        name = clean_text(r.msg.get("name"), 60)
        if name:
            r.device.name = name.strip() or r.device.name
        if "push" in r.msg:
            r.device.push = r.msg["push"] if webpush.valid_subscription(r.msg["push"]) else None
        r.account.save_device(r.device)
        if first and r.account.seq == 0:
            await self.message(r.account, messages.WELCOME)

    async def _say(self, r: Request) -> None:
        text = clean_text(r.msg.get("text"), MAX_TEXT) or ""
        cid = clean_text(r.msg.get("cid"), 64) or ""
        image = photo(r.msg.get("image"))
        if r.msg.get("image") is not None and image is None:
            await r.reply({"t": "error", "code": "bad_image"})
            return
        if not text.strip() and image is None:
            return
        echo = {"t": "msg", "role": "me", "text": text, "cid": cid}
        if image is not None:
            echo["image"] = image.to_dict()
        await self.publish(r.account, echo)
        if self.agent is not None:
            await self.agent.inbound(r.account, text, image)

    async def _approval_reply(self, r: Request) -> None:
        decision = r.msg.get("decision")
        approval_id = clean_text(r.msg.get("id"), 32)
        if decision not in ("approve", "decline", "edit") or not approval_id or self.agent is None:
            return
        note = clean_text(r.msg.get("note"), 2000) or ""
        await self.agent.approval_reply(r.account, approval_id, decision, r.msg.get("values"), note)

    async def _stop(self, r: Request) -> None:
        if self.agent is not None:
            await self.agent.stop(r.account)

    async def _cut(self, r: Request) -> None:
        """[collapse]: the part going on folds now, on every device (SPEC §7.4 "Folding")."""
        await self.publish(r.account, {"t": "cut"})

    async def _sync(self, r: Request) -> None:
        after = r.msg.get("after")
        after = after if isinstance(after, int) and after >= 0 else 0
        since = time.time() - self.history_days * 86400 if after == 0 else 0
        days = r.msg.get("days")
        if after == 0 and isinstance(days, int) and 0 <= days < self.history_days:
            since = time.time() - days * 86400
        items = fit(r.account.items(after=after, since=since, limit=SYNC_LIMIT))
        working = self.agent is not None and self.agent.owes_reply(r.account)  # it may have missed `typing`
        start = r.account.history_start()
        await r.reply({"t": "sync", "items": items, "last": r.account.seq, "start": start, "working": working})

    async def _history(self, r: Request) -> None:
        before, limit = r.msg.get("before"), r.msg.get("limit")
        if not isinstance(before, int) or before < 1:
            return
        limit = limit if isinstance(limit, int) and 1 <= limit <= HISTORY_LIMIT else HISTORY_LIMIT
        await r.reply({"t": "history", "items": fit(r.account.items(before=before, limit=limit))})

    async def _history_settings(self, r: Request) -> None:
        await r.reply({"t": "history_settings", "days": r.account.history_days})

    async def _history_settings_set(self, r: Request) -> None:
        days = r.msg.get("days")
        if days not in HISTORY_DAYS:
            await r.reply({"t": "error", "code": "bad_value"})
            return
        r.account.history_days = days
        r.account.save()
        await self.to_devices(r.account, {"t": "history_settings", "days": days})
        if r.account.prune_history():
            await self.history_changed(r.account)

    async def _history_clear(self, r: Request) -> None:
        if self.agent is not None:
            await self.agent.stop(r.account, quiet=True)
        r.account.clear_history()
        log.info("history cleared for an account")
        await self.history_changed(r.account)

    async def _model(self, r: Request) -> None:
        """Settings → Model (SPEC §3.2): one choice for the household; the new model loads at once."""
        if self.models is None:
            await r.reply({"t": "error", "code": "unknown_type"})
            return
        setting = r.msg.get("t") == "model_set"
        if setting:
            model = clean_text(r.msg.get("model"), 64) or ""
            if not await self.models.set(model):
                await r.reply({"t": "error", "code": "bad_model"})
                return
            r.account.audit({"model_set": model, "via": "settings"})
            if self.agent is not None:
                self._spawn(self.agent.warm())
        listing = await self.models.listing()
        await (self.to_devices(r.account, listing) if setting else r.reply(listing))

    async def _trace(self, r: Request) -> None:
        """A message's ⋯ → Details (SPEC §3.8): what the model wrote, thought and called, with times."""
        seq = r.msg.get("seq")
        if isinstance(seq, int) and seq > 0:
            await r.reply({"t": "trace", "seq": seq, "events": trace(r.account.read_transcript(), seq)})

    async def _memories(self, r: Request) -> None:
        """Settings → What Aha! remembers; Forget is the person's own action, so no card (SPEC §5.4)."""
        if r.msg.get("t") == "memory_forget":
            actions.forget_memory(r.account, clean_text(r.msg.get("id"), 64) or "", via="settings")
        await r.reply({"t": "memories", "items": actions.memories(r.account), "topics": actions.topics(r.account)})

    async def _tasks(self, r: Request) -> None:
        """The Tasks panel (SPEC §3.4 "Background tasks", §7.4): the person's background tasks and the idle time
        the checks wait for. Cancel is the person's own action, so no card."""
        if r.msg.get("t") == "task_cancel":
            actions.cancel_background(r.account, clean_text(r.msg.get("id"), 64) or "", via="settings")
        agent = self.agent
        await r.reply(
            {
                "t": "tasks",
                "items": background.listing(r.account),
                "idle_since": int(agent.last_input) if agent is not None else 0,
                "idle_after": background.IDLE_AFTER,
                "busy": agent.working() if agent is not None else False,
            }
        )

    async def _browser(self, r: Request) -> None:
        """Settings → Your browser (SPEC §3.4, §5.7, §5.8, §7.4): what it remembers, trust, saved logins."""
        t, site = r.msg.get("t"), clean_text(r.msg.get("site"), 255) or ""
        browser = self.agent.browser_actions if self.agent is not None else None
        if t == "login_forget" and site:
            actions.forget_login(r.account, site, via="settings")
        elif t == "browser_trust" and isinstance(r.msg.get("trusted"), bool):
            if not actions.set_trust(r.account, site, r.msg["trusted"], via="settings"):
                await r.reply({"t": "error", "code": "bad_site"})
        elif t == "browser_forget" and site and browser is not None:
            await browser.forget_site(r.account, site, via="settings")
        elif t == "browser_clear" and browser is not None:
            await browser.clear(r.account, via="settings")
        await r.reply({"t": "browser", "sites": await browser.listing(r.account) if browser is not None else []})

    async def _connections(self, r: Request) -> None:
        """Settings → Connections (SPEC §7.4): what this person connected Aha! to. Addresses only, never a token."""
        if r.msg.get("t") == "email_disconnect":
            await actions.disconnect_email(r.account, clean_text(r.msg.get("email"), 320) or "", via="settings")
        await r.reply({"t": "connections", "email": google.address(r.account), "emails": google.addresses(r.account)})

    async def _devices(self, r: Request) -> None:
        await r.reply({"t": "devices", "items": self._device_list(r.account, r.device)})

    async def _revoke_device(self, r: Request) -> None:
        dev = r.msg.get("device")
        if not isinstance(dev, str) or dev not in r.account.devices:
            return
        await self.revoke(r.account, dev)
        if dev != r.device.dev:  # a device that revoked itself gets no answer
            await self._devices(r)

    async def _report_preview(self, r: Request) -> None:
        note = clean_text(r.msg.get("note"), reports.MAX_NOTE) or ""
        app = clean_text(r.msg.get("app"), 20) or ""
        versions = reports.versions(app, __version__, self.models.current if self.models else "")
        count = reports.parse_count(r.msg.get("count"))
        draft = reports.build(r.account.read_transcript(), count, note, r.msg.get("details") is True, versions)
        self.drafts[r.account.id] = draft
        await r.reply({"t": "report_draft", "id": draft.id, "text": draft.text})

    async def _report_send(self, r: Request) -> None:
        draft = self.drafts.get(r.account.id)
        if draft is None or draft.id != r.msg.get("id") or draft.expires < time.time():
            sent_id = clean_text(r.msg.get("id"), 32) or ""
            await r.reply({"t": "report_sent", "id": sent_id, "ok": False, "code": "expired"})
            return
        del self.drafts[r.account.id]
        if "report" not in self.gateway_features:  # an older or self-hosted gateway without reports
            await r.reply({"t": "report_sent", "id": draft.id, "ok": False, "code": "reports_off"})
            return
        self._spawn(self._send_report(r.account, r.device, draft))

    async def _send_report(self, account: Account, device: Device, draft: reports.Draft) -> None:
        """To the gateway, which opens a private issue and keeps nothing (SPEC §7.7)."""
        fut = asyncio.get_running_loop().create_future()
        self._report_waiters[draft.id] = fut
        try:
            await self.send_frame(Report(id=draft.id, title=draft.title, body=draft.text))
            result: ReportResult = await asyncio.wait_for(fut, 30)
            ok, code = result.ok, result.code
        except (ConnectionError, TimeoutError):
            ok, code = False, "gateway_unreachable"
        finally:
            self._report_waiters.pop(draft.id, None)
        log.info("problem report %s", "sent" if ok else f"not sent ({code})")
        out = {"t": "report_sent", "id": draft.id, "ok": ok}
        await self._send(account, device, out if ok else {**out, "code": code})

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def on_report_result(self, frame: ReportResult) -> None:
        fut = self._report_waiters.get(frame.id)
        if fut is not None and not fut.done():
            fut.set_result(frame)

    def _device_list(self, account: Account, current: Device) -> list[dict]:
        return [
            {"id": d.dev, "name": d.name, "created": d.created, "last_seen": d.last_seen, "this": d.dev == current.dev}
            for d in account.devices.values()
        ]

    async def revoke(self, account: Account, dev: str) -> None:
        self.accounts.remove_device(account, dev)
        self.online.discard(dev)
        with contextlib.suppress(ConnectionError):
            await self.send_frame(Revoke(dev=dev))

    # ---- pairing (SPEC §6.4)

    def pairing_link(self, code: str, state: PairingState, account: Account, pin: str) -> str:
        data = {
            "v": 1,
            "gw": self.device_url,
            "pin": pin,
            "fp": crypto.b64url(self.identity.fingerprint()),
            "code": code,
            "psk": crypto.b64url(state.offer.psk),
            "name": account.name,
            "home": self.household,
        }
        return f"{self.app_url}/#pair=" + crypto.b64url(json.dumps(data, separators=(",", ":")).encode())

    def new_pairing(self, code: str, account: Account) -> PairingState:
        now = time.monotonic()
        self.pairings = {c: s for c, s in self.pairings.items() if s.expires > now}
        state = PairingState(
            account.id, crypto.PairingOffer.new(), now + PAIR_LIFETIME, asyncio.get_running_loop().create_future()
        )
        self.pairings[code] = state
        return state

    async def on_pair(self, frame: Pair) -> None:
        state = self.pairings.get(frame.code)
        if state is None or state.expires <= time.monotonic():
            return
        account = self.accounts.get(state.account_id)
        if account is None:
            return
        kind = frame.body.get("t")
        if kind == "hello":
            body = {"t": "bundle", **crypto.computer_bundle(self.identity, state.offer)}
            await self.send_frame(Pair(code=frame.code, body=body))
        elif kind == "finish":
            try:
                paired = crypto.computer_finish(self.identity, state.offer, frame.body)
            except crypto.CryptoError:
                log.warning("a pairing attempt failed its proof; the code is now closed")
                self.pairings.pop(frame.code, None)
                await self.send_frame(Pair(code=frame.code, body={"t": "error", "code": "proof"}))
                if not state.done.done():
                    state.done.set_exception(RuntimeError("pairing proof failed"))
                return
            self.pairings.pop(frame.code, None)  # one device per code
            dev = crypto.b64e(paired.sigd)
            ad = crypto.session_ad(paired.ikd, self.identity.ik_pub)
            ratchet = crypto.Ratchet.init_computer(
                paired.keys.sk, ad, state.offer.ek_priv, state.offer.ek_pub, self.identity.kem_seed, paired.kemd
            )
            cert = await self.request_cert(dev, account.tag, code=frame.code)
            device = Device(
                dev=dev,
                name="New device",
                ikd=crypto.b64e(paired.ikd),
                kemd=crypto.b64e(paired.kemd),
                push_key=crypto.b64e(paired.keys.push_key),
                ratchet=ratchet.to_dict(),
                cert=cert.model_dump(),
            )
            self.accounts.add_device(account, device)
            body = {"t": "ok", "cert": cert.model_dump_json(), "mac": crypto.b64e(paired.keys.computer_mac())}
            await self.send_frame(Pair(code=frame.code, body=body))
            if not state.done.done():
                state.done.set_result(account.label)
            log.info("a device was paired")

    async def request_cert(self, dev: str, tag: str, timeout: float = 15, code: str | None = None) -> Cert:
        request_id = secrets.token_hex(8)
        waiter = asyncio.get_running_loop().create_future()
        self._cert_waiters[request_id] = waiter
        try:
            await self.send_frame(CertRequest(id=request_id, device=dev, tag=tag, code=code))
            return await asyncio.wait_for(waiter, timeout)
        finally:
            self._cert_waiters.pop(request_id, None)

    async def _renew(self, account: Account, device: Device) -> None:
        try:
            cert = await self.request_cert(device.dev, account.tag)
        except (TimeoutError, ConnectionError):
            return
        device.cert = cert.model_dump()
        account.save_device(device)
        await self._send(account, device, {"t": "cert", "cert": cert.model_dump()})


# What each device message does (SPEC §6.5): one handler per type, the only place the types are listed here.
HANDLERS: dict[str, Callable[[Channel, Request], Awaitable[None]]] = {
    "hello": Channel._hello,
    "say": Channel._say,
    "approval_reply": Channel._approval_reply,
    "stop": Channel._stop,
    "cut": Channel._cut,
    "sync_request": Channel._sync,
    "history_request": Channel._history,
    "history_settings_request": Channel._history_settings,
    "history_settings_set": Channel._history_settings_set,
    "history_clear": Channel._history_clear,
    "model_request": Channel._model,
    "model_set": Channel._model,
    "trace_request": Channel._trace,
    "memories_request": Channel._memories,
    "memory_forget": Channel._memories,
    "tasks_request": Channel._tasks,
    "task_cancel": Channel._tasks,
    "browser_request": Channel._browser,
    "browser_forget": Channel._browser,
    "browser_clear": Channel._browser,
    "browser_trust": Channel._browser,
    "login_forget": Channel._browser,
    "connections_request": Channel._connections,
    "email_disconnect": Channel._connections,
    "devices_request": Channel._devices,
    "revoke_device": Channel._revoke_device,
    "report_preview": Channel._report_preview,
    "report_send": Channel._report_send,
}
