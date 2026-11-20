"""Accounts and their files (SPEC §3.8). An account is one chat address; nothing here is ever uploaded."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .protocol import Cert


def account_id(address: str) -> str:
    """File names never hold phone numbers."""
    return hashlib.sha256(address.encode()).hexdigest()[:24]


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def _append_jsonl(path: Path, entry: dict) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
    with path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass
class Account:
    address: str
    dir: Path
    name: str = ""
    cert: Cert | None = None

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def label(self) -> str:
        return self.name or self.address.split(":", 1)[-1]

    # ---- files

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            self.dir / "account.json",
            {
                "address": self.address,
                "name": self.name,
                "cert": self.cert.model_dump() if self.cert else None,
            },
        )

    def transcript(self, entry: dict) -> None:
        _append_jsonl(self.dir / "transcript.jsonl", entry)

    def audit(self, entry: dict) -> None:
        _append_jsonl(self.dir / "audit.jsonl", entry)

    def read_transcript(self) -> list[dict]:
        path = self.dir / "transcript.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def load_context(self) -> dict:
        path = self.dir / "context.json"
        return json.loads(path.read_text()) if path.exists() else {"summary": "", "turns": []}

    def save_context(self, context: dict) -> None:
        _write_json(self.dir / "context.json", context)

    def load_memory(self) -> list[dict]:
        path = self.dir / "memory.json"
        return json.loads(path.read_text()) if path.exists() else []

    def save_memory(self, items: list[dict]) -> None:
        _write_json(self.dir / "memory.json", items)


class Accounts:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "accounts"
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Account] = {}
        for d in sorted(self.root.iterdir()):
            f = d / "account.json"
            if f.exists():
                raw = json.loads(f.read_text())
                acct = Account(
                    raw["address"],
                    d,
                    raw.get("name", ""),
                    Cert.model_validate(raw["cert"]) if raw.get("cert") else None,
                )
                self._cache[acct.address] = acct

    def get(self, address: str) -> Account | None:
        return self._cache.get(address)

    def all(self) -> list[Account]:
        return list(self._cache.values())

    def paired(self) -> list[Account]:
        return [a for a in self._cache.values() if a.cert is not None]

    def upsert(self, address: str, cert: Cert | None = None, name: str = "") -> Account:
        acct = self._cache.get(address) or Account(address, self.root / account_id(address))
        if cert is not None:
            acct.cert = cert
        if name and not acct.name:
            acct.name = name
        acct.save()
        self._cache[address] = acct
        return acct

    def unpair(self, address: str) -> Account | None:
        acct = self._cache.get(address)
        if acct:
            acct.cert = None
            acct.save()
        return acct

    def delete(self, address: str) -> bool:
        acct = self._cache.pop(address, None)
        if acct is None:
            return False
        shutil.rmtree(acct.dir, ignore_errors=True)
        return True

    def export(self, address: str, target: Path) -> Path | None:
        acct = self._cache.get(address)
        if acct is None:
            return None
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(acct.dir.iterdir()):
                if f.is_file() and not f.name.endswith(".tmp"):
                    z.write(f, f.name)
        return target
