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
import hashlib
import json
import os
import secrets
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .protocol import Cert
from .rest import Vault

VERSION = 2


def _log_line(entry: dict) -> bytes:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
    return json.dumps(entry, ensure_ascii=False).encode()


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
            {"v": VERSION, "id": self.id, "name": self.name, "tag": self.tag, "created": self.created, "seq": self.seq},
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
        self.vault.append(self.dir / "transcript.jsonl", _log_line(entry))

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
        out = []
        for e in self.read_transcript():
            item = e.get("item") if e.get("kind") == "item" else None
            if not item or item.get("seq", 0) <= after or (before is not None and item["seq"] >= before):
                continue
            if since and item.get("ts", 0) < since:
                continue
            out.append(item)
        return out[-limit:]

    def load_context(self) -> dict:
        return self.vault.read_json(self.dir / "context.json", {"summary": "", "turns": []})

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
