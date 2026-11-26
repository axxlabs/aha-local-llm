# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Encryption at rest for the data folder (SPEC §3.8.1).

One 256-bit data key per computer, unsealed by systemd from a TPM-bound credential and handed to the agent as a
read-only file. Each file has its own key, HKDF(data key, its path in the data folder), so files can't be
swapped. The AEAD is ChaCha20-Poly1305 with a 24-byte random nonce: its first 16 bytes derive a subkey, the last
8 complete the cipher's nonce, so random nonces never repeat in practice.

- Whole files: b"AHAR1" | nonce(24) | ciphertext, written to a temporary file and renamed.
- Append-only logs: b"AHAL1", then records: length(4, big-endian) | nonce(24) | ciphertext, with the record's
  number as associated data. A record cut short at the end (a crash mid-write) is ignored and overwritten.

A file that doesn't decrypt is an error, never read as plaintext.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

WHOLE = b"AHAR1"
LOG = b"AHAL1"
NONCE = 24
KEY_BYTES = 32
FORMAT_FILE = "format.json"
# Not encrypted (SPEC §3.8.1): the format marker, the socket, exports (the person's own plaintext copy),
# temporary files.
PLAIN = {FORMAT_FILE, "agent.sock"}
PLAIN_DIRS = ("exports/",)
LOGS = (".jsonl",)


class RestError(Exception):
    """A data file that is missing its encryption, altered, or under another key."""


def _hkdf(key: bytes, info: bytes) -> bytes:
    return HKDF(hashes.SHA256(), KEY_BYTES, None, info).derive(key)


def recovery_code(key: bytes) -> str:
    text = base64.b32encode(key).decode().rstrip("=")
    return " ".join(text[i : i + 4] for i in range(0, len(text), 4))


def parse_recovery_code(code: str) -> bytes:
    text = "".join(code.split()).replace("-", "").upper()
    try:
        key = base64.b32decode(text + "=" * (-len(text) % 8))
    except ValueError as e:
        raise ValueError("that isn't a recovery code") from e
    if len(key) != KEY_BYTES:
        raise ValueError("that isn't a recovery code")
    return key


class Vault:
    def __init__(self, root: Path, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise RestError("the data key must be 32 bytes")
        self.root = root
        self._key = key
        self._file_keys: dict[str, bytes] = {}
        self._logs: dict[Path, tuple[int, int]] = {}  # valid end offset, record count

    @classmethod
    def open(cls, root: Path, key_file: Path) -> Vault:
        """The vault for the data folder, brought to today's storage version first (SPEC §3.8.3): the migrations
        run before anything reads it."""
        if not key_file.exists():
            raise RestError(
                f"no data key at {key_file}: the unit unseals it from ~/.config/aha/data-key.cred "
                "(run `aha install` to create one)"
            )
        vault = cls(root, key_file.read_bytes())
        from .migrations import upgrade

        upgrade(vault)
        return vault

    # ---- keys and AEAD

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError as e:
            raise RestError(f"{path} is outside the data folder") from e

    def _file_key(self, path: Path) -> bytes:
        rel = self._rel(path)
        if rel not in self._file_keys:
            self._file_keys[rel] = _hkdf(self._key, b"aha-rest-v1|" + rel.encode())
        return self._file_keys[rel]

    @staticmethod
    def _cipher(file_key: bytes, nonce: bytes) -> tuple[ChaCha20Poly1305, bytes]:
        sub = _hkdf(file_key, b"aha-rest-v1-sub|" + nonce[:16])
        return ChaCha20Poly1305(sub), b"\0\0\0\0" + nonce[16:]

    def _seal(self, file_key: bytes, data: bytes, ad: bytes) -> bytes:
        nonce = secrets.token_bytes(NONCE)
        aead, n = self._cipher(file_key, nonce)
        return nonce + aead.encrypt(n, data, ad)

    def _open(self, file_key: bytes, blob: bytes, ad: bytes, path: Path) -> bytes:
        if len(blob) < NONCE + 16:
            raise RestError(f"{self._rel(path)} doesn't decrypt (too short)")
        aead, n = self._cipher(file_key, blob[:NONCE])
        try:
            return aead.decrypt(n, blob[NONCE:], ad)
        except InvalidTag as e:
            raise RestError(f"{self._rel(path)} doesn't decrypt (altered, moved, or another computer's key)") from e

    # ---- whole files

    def read(self, path: Path) -> bytes | None:
        if not path.exists():
            return None
        raw = path.read_bytes()
        if not raw.startswith(WHOLE):
            raise RestError(f"{self._rel(path)} isn't encrypted")
        return self._open(self._file_key(path), raw[len(WHOLE) :], WHOLE, path)

    def read_json(self, path: Path, default=None):
        data = self.read(path)
        return default if data is None else json.loads(data)

    def write(self, path: Path, data: bytes) -> None:
        blob = WHOLE + self._seal(self._file_key(path), data, WHOLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)

    def write_json(self, path: Path, data) -> None:
        self.write(path, json.dumps(data, ensure_ascii=False, indent=1).encode())

    def create(self, path: Path, data: bytes) -> None:
        """Write a file that must not exist yet (keys)."""
        if path.exists():
            raise FileExistsError(path)
        self.write(path, data)

    # ---- append-only logs

    def _scan(self, path: Path) -> tuple[int, int]:
        if path in self._logs:
            return self._logs[path]
        if not path.exists():
            state = (0, 0)
        else:
            raw = path.read_bytes()
            if not raw.startswith(LOG):
                raise RestError(f"{self._rel(path)} isn't an encrypted log")
            offset, count = len(LOG), 0
            while offset + 4 <= len(raw):
                (n,) = struct.unpack(">I", raw[offset : offset + 4])
                if offset + 4 + n > len(raw):
                    break  # cut short at the end
                offset += 4 + n
                count += 1
            state = (offset, count)
        self._logs[path] = state
        return state

    def append(self, path: Path, data: bytes) -> None:
        offset, count = self._scan(path)
        record = self._seal(self._file_key(path), data, struct.pack(">Q", count))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as f:
            if offset == 0:
                f.write(LOG)
                offset = len(LOG)
            f.seek(offset)
            f.write(struct.pack(">I", len(record)) + record)
            f.truncate()
            self._logs[path] = (f.tell(), count + 1)

    def records(self, path: Path) -> list[bytes]:
        if not path.exists():
            return []
        end, count = self._scan(path)
        raw = path.read_bytes()[:end]
        key = self._file_key(path)
        out, offset = [], len(LOG)
        for i in range(count):
            (n,) = struct.unpack(">I", raw[offset : offset + 4])
            out.append(self._open(key, raw[offset + 4 : offset + 4 + n], struct.pack(">Q", i), path))
            offset += 4 + n
        return out

    def rewrite(self, path: Path, records: list[bytes]) -> None:
        """Replace a log with these records, sealed again at their new positions (SPEC §3.8.2)."""
        if not records:
            path.unlink(missing_ok=True)
            self._logs.pop(path, None)
            return
        key = self._file_key(path)
        blob = bytearray(LOG)
        for i, data in enumerate(records):
            record = self._seal(key, data, struct.pack(">Q", i))
            blob += struct.pack(">I", len(record)) + record
        self._replace_synced(path, bytes(blob))
        self._logs[path] = (len(blob), len(records))

    def forget(self, path: Path) -> None:
        """Drop cached log state for files removed from disk (account deletion)."""
        for p in [p for p in self._logs if p == path or path in p.parents]:
            del self._logs[p]

    # ---- upgrade from plaintext

    def _is_plain(self, rel: str) -> bool:
        return rel in PLAIN or rel.startswith(PLAIN_DIRS) or rel.endswith(".tmp")

    def encrypt_plaintext(self) -> int:
        """Encrypt every plaintext file in place (migration 0001); idempotent, so a crash halfway is finished at
        the next start."""
        self.root.mkdir(parents=True, exist_ok=True)
        done = 0
        for path in sorted(p for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = self._rel(path)
            if self._is_plain(rel):
                continue
            raw = path.read_bytes()
            if raw.startswith((WHOLE, LOG)):
                continue
            if rel.endswith(LOGS):
                lines = [line.encode() for line in raw.decode().splitlines() if line.strip()]
                blob = bytearray(LOG)
                for i, line in enumerate(lines):
                    record = self._seal(self._file_key(path), line, struct.pack(">Q", i))
                    blob += struct.pack(">I", len(record)) + record
                self._replace_synced(path, bytes(blob))
                self._logs[path] = (len(blob), len(lines))
            else:
                self._replace_synced(path, WHOLE + self._seal(self._file_key(path), raw, WHOLE))
            done += 1
        for path in self.root.rglob("*"):
            if path.is_dir():
                os.chmod(path, 0o700)
        os.chmod(self.root, 0o700)
        return done

    @staticmethod
    def _replace_synced(path: Path, blob: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def load_or_create_instance_key(vault: Vault, path: Path):
    """The computer's Ed25519 instance key (SPEC §6.4), encrypted at rest. Lives here, not in certs.py, because
    certs.py is shared byte-for-byte with the gateway."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = vault.read(path)
    if raw is not None:
        return Ed25519PrivateKey.from_private_bytes(raw)
    key = Ed25519PrivateKey.generate()
    vault.create(
        path,
        key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()),
    )
    return key
