# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Accounts (people), their devices and their files (SPEC §3.8). Nothing here is ever uploaded.

Every file is encrypted at rest through the data folder's vault (SPEC §3.8.1).

An account is a person on this computer: a random id, a display name, a random routing tag (the gateway counts
tags for billing and learns nothing else), memories, the transcript and the context. Each device of the account
has its own file under `devices/` (mode 0600) with its public keys, push subscription, binding certificate and
ratchet state.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import time
import zipfile
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .protocol import Cert
from .vault import Vault

# The task running in this asyncio context (SPEC §3.8): the records written while it runs carry its id, and its
# Details line is named by it. A context variable, not a field on the account: only what runs inside the task
# (its tools, its messages) is tagged, never something written for the same account from elsewhere meanwhile.
TASK: ContextVar[str | None] = ContextVar("aha_task", default=None)

VERSION = 2


def _log_line(entry: dict) -> bytes:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
    return json.dumps(entry, ensure_ascii=False).encode()


def _epoch(stamp) -> float | None:
    """A time as written in the logs or the context: epoch seconds, or ISO local time with or without an offset."""
    if isinstance(stamp, (int, float)):
        return float(stamp)
    try:
        return dt.datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError:
        return None


def turn_time(turn: dict) -> float:
    """When a turn of the conversation context was recorded, in epoch seconds (0 if unknown)."""
    return _epoch(turn.get("t", turn.get("ts"))) or 0.0


def _before(stamp, cutoff: float) -> bool:
    t = _epoch(stamp)
    return t is not None and t < cutoff


def _older(record: bytes, cutoff: float) -> bool:
    try:
        entry = json.loads(record)
    except json.JSONDecodeError:
        return False
    item = entry.get("item") if isinstance(entry.get("item"), dict) else {}
    return _before(item.get("ts", entry.get("ts")), cutoff)


def device_file_id(dev: str) -> str:
    return hashlib.sha256(dev.encode()).hexdigest()[:20]


@dataclass
class Device:
    dev: str  # the device's Ed25519 auth key (SIGd), base64: its id everywhere
    name: str
    ikd: str
    kemd: str
    push_key: str
    ratchet: dict
    cert: dict | None = None
    push: dict | None = None  # Web Push subscription {endpoint, keys: {p256dh, auth}}
    created: int = field(default_factory=lambda: int(time.time()))
    last_seen: int = 0

    @property
    def certificate(self) -> Cert | None:
        return Cert.model_validate(self.cert) if self.cert else None


RECENT = 2000  # chat items kept in memory per person: far more than one part of the chat


def _brief(item: dict) -> dict:
    return {k: item[k] for k in ("seq", "ts", "t", "from", "new") if k in item}


@dataclass
class Account:
    id: str
    dir: Path
    name: str
    tag: str
    vault: Vault = field(repr=False)
    created: int = field(default_factory=lambda: int(time.time()))
    seq: int = 0
    devices: dict[str, Device] = field(default_factory=dict)
    history_days: int | None = None  # how long the computer keeps this person's history; None: forever (§3.8.1)
    # The latest chat items, in short (seq, ts, t, from, new): read from the transcript once, then kept up to date
    # as items are written, so finding the current part of the chat (SPEC §7.4) never reads the whole history.
    _recent: list[dict] | None = field(default=None, repr=False, compare=False)

    @property
    def address(self) -> str:
        """The key the agent uses for per-person state (running tasks, approvals)."""
        return self.id

    @property
    def label(self) -> str:
        return self.name or self.id[:8]

    # ---- files

    def save(self) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.vault.write_json(
            self.dir / "account.json",
            {
                "v": VERSION,
                "id": self.id,
                "name": self.name,
                "tag": self.tag,
                "created": self.created,
                "seq": self.seq,
                **({"history_days": self.history_days} if self.history_days else {}),
            },
        )

    def next_seq(self) -> int:
        self.seq += 1
        self.save()
        return self.seq

    def save_device(self, device: Device) -> None:
        d = self.dir / "devices"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        self.vault.write_json(d / f"{device_file_id(device.dev)}.json", asdict(device))
        self.devices[device.dev] = device

    def remove_device(self, dev: str) -> Device | None:
        device = self.devices.pop(dev, None)
        if device is not None:
            (self.dir / "devices" / f"{device_file_id(dev)}.json").unlink(missing_ok=True)
        return device

    def transcript(self, entry: dict) -> None:
        entry = {**entry, "task": task} if (task := TASK.get()) else entry
        self.vault.append(self.dir / "transcript.jsonl", _log_line(entry))
        if self._recent is not None and entry.get("kind") == "item" and isinstance(entry.get("item"), dict):
            self._recent.append(_brief(entry["item"]))
            del self._recent[:-RECENT]

    def recent_items(self) -> list[dict]:
        """The latest chat items, in short (SPEC §7.4): the transcript is read only the first time."""
        if self._recent is None:
            items = [
                r["item"] for r in self.read_transcript() if r.get("kind") == "item" and isinstance(r.get("item"), dict)
            ]
            self._recent = [_brief(it) for it in items[-RECENT:]]
        return list(self._recent)

    def audit(self, entry: dict) -> None:
        self.vault.append(self.dir / "audit.jsonl", _log_line(entry))

    def read_transcript(self) -> list[dict]:
        out = []
        for line in self.vault.records(self.dir / "transcript.jsonl"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def items(self, after: int = 0, before: int | None = None, since: float = 0, limit: int = 10_000) -> list[dict]:
        """Items shown in the app (messages, approval cards and their states), by sequence number."""
        from . import numbers

        out = []
        for e in self.read_transcript():
            item = e.get("item") if e.get("kind") == "item" else None
            if not item or item.get("seq", 0) <= after or (before is not None and item["seq"] >= before):
                continue
            if since and item.get("ts", 0) < since:
                continue
            out.append(item)
        # A title carries its part's number (SPEC §7.4 "Topic numbers"): from numbers.json, not the item.
        known = numbers.parts(self) if any(it.get("t") == "title" for it in out) else {}
        out = [
            {**it, "n": known[it["from"]]} if it.get("t") == "title" and it.get("from") in known else it for it in out
        ]
        return out[-limit:]

    # ---- history length and clearing (SPEC §3.8.1)

    def history_start(self) -> int:
        """The first sequence number still held: anything before it was deleted."""
        for e in self.read_transcript():
            if e.get("kind") == "item" and isinstance(e.get("item", {}).get("seq"), int):
                return e["item"]["seq"]
        return self.seq + 1

    def _keep_log(self, name: str, cutoff: float) -> bool:
        path = self.dir / name
        records = self.vault.records(path)
        kept = [r for r in records if not _older(r, cutoff)]
        if len(kept) == len(records):
            return False
        self.vault.rewrite(path, kept)
        return True

    def prune_history(self, now: float | None = None) -> bool:
        """Delete history older than the person's chosen length. True if anything was deleted."""
        if not self.history_days:
            return False
        cutoff = (time.time() if now is None else now) - self.history_days * 86400
        changed = self._keep_log("transcript.jsonl", cutoff)
        self._recent = None  # read again when needed
        changed = self._keep_log("audit.jsonl", cutoff) or changed
        conv = self.load_context()
        turns = [t for t in conv.get("turns", []) if not _before(t.get("t", t.get("ts")), cutoff)]
        topics = [e for e in conv.get("topics", []) if not _before(e.get("ts"), cutoff)]
        if len(turns) != len(conv.get("turns", [])) or len(topics) != len(conv.get("topics", [])):
            conv["turns"], conv["topics"] = turns, topics
            self.save_context(conv)
            changed = True
        return changed

    def clear_history(self) -> None:
        """Delete the transcript, the audit and the conversation context. Sequence numbers carry on."""
        for name in ("transcript.jsonl", "audit.jsonl"):
            self.vault.rewrite(self.dir / name, [])
        self._recent = None
        (self.dir / "context.json").unlink(missing_ok=True)

    def load_context(self) -> dict:
        """The turns of the topic going on and the topic memory (SPEC §4, §5.4)."""
        conv = self.vault.read_json(self.dir / "context.json", None) or {"turns": [], "topics": []}
        conv.setdefault("turns", [])
        conv.setdefault("topics", [])
        return conv

    def save_context(self, context: dict) -> None:
        self.vault.write_json(self.dir / "context.json", context)

    def load_memory(self) -> list[dict]:
        return self.vault.read_json(self.dir / "memory.json", [])

    def save_memory(self, items: list[dict]) -> None:
        self.vault.write_json(self.dir / "memory.json", items)


class Accounts:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.root = vault.root / "accounts"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._cache: dict[str, Account] = {}
        self._by_device: dict[str, str] = {}
        for d in sorted(self.root.iterdir()):
            f = d / "account.json"
            if not f.exists():
                continue
            raw = vault.read_json(f)
            if raw.get("v") != VERSION:
                continue  # v1 (chat-app address) accounts are not loaded
            acct = Account(
                raw["id"], d, raw.get("name", ""), raw["tag"], vault, raw.get("created", 0), raw.get("seq", 0)
            )
            days = raw.get("history_days")
            acct.history_days = days if isinstance(days, int) and days > 0 else None
            for df in sorted((d / "devices").glob("*.json")) if (d / "devices").exists() else []:
                device = Device(**vault.read_json(df))
                acct.devices[device.dev] = device
                self._by_device[device.dev] = acct.id
            self._cache[acct.id] = acct

    def create(self, name: str) -> Account:
        account_id = secrets.token_hex(16)
        tag = base64.b64encode(secrets.token_bytes(16)).decode()
        acct = Account(account_id, self.root / account_id, name.strip()[:60], tag, self.vault)
        acct.save()
        self._cache[acct.id] = acct
        return acct

    def get(self, account_id: str) -> Account | None:
        return self._cache.get(account_id)

    def find(self, key: str) -> Account | None:
        """By id, id prefix (8+ characters) or exact name (case-insensitive)."""
        if key in self._cache:
            return self._cache[key]
        matches = [a for a in self._cache.values() if a.name.lower() == key.lower()]
        if not matches and len(key) >= 8:
            matches = [a for a in self._cache.values() if a.id.startswith(key)]
        return matches[0] if len(matches) == 1 else None

    def by_device(self, dev: str) -> tuple[Account, Device] | None:
        account_id = self._by_device.get(dev)
        acct = self._cache.get(account_id) if account_id else None
        if acct is None or dev not in acct.devices:
            return None
        return acct, acct.devices[dev]

    def add_device(self, acct: Account, device: Device) -> None:
        acct.save_device(device)
        self._by_device[device.dev] = acct.id

    def remove_device(self, acct: Account, dev: str) -> Device | None:
        self._by_device.pop(dev, None)
        return acct.remove_device(dev)

    def all(self) -> list[Account]:
        return list(self._cache.values())

    def certs(self) -> list[Cert]:
        return [d.certificate for a in self._cache.values() for d in a.devices.values() if d.cert]

    def rename(self, acct: Account, name: str) -> None:
        acct.name = name.strip()[:60]
        acct.save()

    def delete(self, account_id: str) -> Account | None:
        acct = self._cache.pop(account_id, None)
        if acct is None:
            return None
        for dev in acct.devices:
            self._by_device.pop(dev, None)
        shutil.rmtree(acct.dir, ignore_errors=True)
        self.vault.forget(acct.dir)
        return acct

    def export(self, account_id: str, target: Path) -> Path | None:
        """The person's own data, decrypted (it's their copy); device keys and ratchet states are left out."""
        acct = self._cache.get(account_id)
        if acct is None:
            return None
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(acct.dir.iterdir()):
                if not f.is_file() or f.name.endswith(".tmp"):
                    continue
                if f.suffix == ".jsonl":
                    z.writestr(f.name, b"".join(r + b"\n" for r in self.vault.records(f)))
                else:
                    z.writestr(f.name, self.vault.read(f) or b"")
        return target
