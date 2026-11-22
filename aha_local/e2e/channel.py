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

from .. import __version__, messages, reports
from ..accounts import Account, Accounts, Device
from ..certs import needs_renewal
from ..items import Image
from ..protocol import E2E, Cert, CertIssued, CertRequest, DeviceStatus, Pair, Push, Report, ReportResult, Revoke, b64
from . import crypto, webpush

log = logging.getLogger("aha.channel")

PAIR_LIFETIME = 600
MAX_TEXT = 20_000
SYNC_LIMIT = 500
HISTORY_LIMIT = 50
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
        model: str = "",
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
        self.model = model
        self.drafts: dict[str, reports.Draft] = {}  # by account id: the report the person is looking at (§7.7)
        self._report_waiters: dict[str, asyncio.Future] = {}

    # ---- output for the agent (agent.AgentOut, approvals.Out)

    async def message(self, account: Account, text: str, image: Image | None = None) -> None:
        item: dict = {"t": "msg", "role": "aha", "text": text}
        if image is not None:
            item["image"] = image.to_dict()
        await self.publish(account, item, notify=True)

    async def text(self, account: Account, text: str) -> None:
        await self.message(account, text)

    async def card(self, account: Account, card: dict) -> None:
        await self.publish(account, card, notify=True)

    async def state(self, account: Account, approval_id: str, state: str) -> None:
        await self.publish(account, {"t": "approval_state", "id": approval_id, "state": state})

    async def typing(self, account: Account, on: bool) -> None:
        for device in list(account.devices.values()):
            if device.dev in self.online:
                await self._send(account, device, {"t": "typing", "on": on})

    async def publish(self, account: Account, item: dict, notify: bool = False) -> dict:
        """Number the item, keep it in the transcript, send it to every device of the account."""
        item = {**item, "seq": account.next_seq(), "ts": int(time.time())}
        account.transcript({"kind": "item", "item": item})
        for device in list(account.devices.values()):
            if device.dev in self.online:
                await self._send(account, device, item)
            elif notify:
                await self._push(account, device, item)
        return item

    async def to_devices(self, account: Account, message: dict) -> None:
        """To every connected device of the account, outside the transcript (settings, not chat)."""
        for device in list(account.devices.values()):
            if device.dev in self.online:
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
        t = msg.get("t")
        reply = lambda m: self._send(account, device, m)  # noqa: E731
        if t == "hello":
            first = device.name == "New device"
            name = clean_text(msg.get("name"), 60)
            if name:
                device.name = name.strip() or device.name
            if "push" in msg:
                device.push = msg["push"] if webpush.valid_subscription(msg["push"]) else None
            account.save_device(device)
            if first and account.seq == 0:
                await self.message(account, messages.WELCOME)
        elif t == "say":
            text = clean_text(msg.get("text"), MAX_TEXT) or ""
            cid = clean_text(msg.get("cid"), 64) or ""
            image = photo(msg.get("image"))
            if msg.get("image") is not None and image is None:
                await reply({"t": "error", "code": "bad_image"})
                return
            if not text.strip() and image is None:
                return
            echo = {"t": "msg", "role": "me", "text": text, "cid": cid}
            if image is not None:
                echo["image"] = image.to_dict()
            await self.publish(account, echo)
            if self.agent is not None:
                await self.agent.inbound(account, text, image)
        elif t == "approval_reply":
            decision = msg.get("decision")
            approval_id = clean_text(msg.get("id"), 32)
            if decision not in ("approve", "decline", "edit") or not approval_id or self.agent is None:
                return
            values = msg.get("values")
            note = clean_text(msg.get("note"), 2000) or ""
            await self.agent.approval_reply(account, approval_id, decision, values, note)
        elif t == "stop":
            if self.agent is not None:
                await self.agent.stop(account)
        elif t == "sync_request":
            after = msg.get("after")
            after = after if isinstance(after, int) and after >= 0 else 0
            since = time.time() - self.history_days * 86400 if after == 0 else 0
            days = msg.get("days")
            if after == 0 and isinstance(days, int) and 0 <= days < self.history_days:
                since = time.time() - days * 86400
            items = account.items(after=after, since=since, limit=SYNC_LIMIT)
            await reply({"t": "sync", "items": items, "last": account.seq, "start": account.history_start()})
        elif t == "history_request":
            before = msg.get("before")
            limit = msg.get("limit")
            if not isinstance(before, int) or before < 1:
                return
            limit = limit if isinstance(limit, int) and 1 <= limit <= HISTORY_LIMIT else HISTORY_LIMIT
            await reply({"t": "history", "items": account.items(before=before, limit=limit)})
        elif t == "history_settings_request":
            await reply({"t": "history_settings", "days": account.history_days})
        elif t == "history_settings_set":
            days = msg.get("days")
            if days not in HISTORY_DAYS:
                await reply({"t": "error", "code": "bad_value"})
                return
            account.history_days = days
            account.save()
            await self.to_devices(account, {"t": "history_settings", "days": days})
            if account.prune_history():
                await self.history_changed(account)
        elif t == "history_clear":
            if self.agent is not None:
                await self.agent.stop(account, quiet=True)
            account.clear_history()
            log.info("history cleared for an account")
            await self.history_changed(account)
        elif t == "memories_request":
            items = [{"id": m.get("id"), "text": m.get("text")} for m in account.load_memory()]
            await reply({"t": "memories", "items": items})
        elif t == "devices_request":
            await reply({"t": "devices", "items": self._device_list(account, device)})
        elif t == "revoke_device":
            dev = msg.get("device")
            if isinstance(dev, str) and dev in account.devices:
                if dev != device.dev:
                    await self.revoke(account, dev)
                    await reply({"t": "devices", "items": self._device_list(account, device)})
                else:
                    await self.revoke(account, dev)
        elif t == "report_preview":
            note = clean_text(msg.get("note"), reports.MAX_NOTE) or ""
            app = clean_text(msg.get("app"), 20) or ""
            versions = reports.versions(app, __version__, self.model)
            draft = reports.build(
                account.read_transcript(),
                reports.parse_count(msg.get("count")),
                note,
                msg.get("details") is True,
                versions,
            )
            self.drafts[account.id] = draft
            await reply({"t": "report_draft", "id": draft.id, "text": draft.text})
        elif t == "report_send":
            draft = self.drafts.get(account.id)
            if draft is None or draft.id != msg.get("id") or draft.expires < time.time():
                await reply(
                    {"t": "report_sent", "id": clean_text(msg.get("id"), 32) or "", "ok": False, "code": "expired"}
                )
                return
            del self.drafts[account.id]
            if "report" not in self.gateway_features:  # an older or self-hosted gateway without reports
                await reply({"t": "report_sent", "id": draft.id, "ok": False, "code": "reports_off"})
                return
            self._spawn(self._send_report(account, device, draft))
        else:
            await reply({"t": "error", "code": "unknown_type"})

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
