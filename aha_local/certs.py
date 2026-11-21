# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Ed25519 keys, binding certificates and handshake signatures (SPEC §6.1–§6.4).

This file is identical in aha-gateway and aha-local-llm.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .protocol import DEVICE_CONTEXT, GATEWAY_PROOF_CONTEXT, HANDSHAKE_CONTEXT, Cert, b64, unb64

CERT_LIFETIME = 365 * 24 * 3600
RENEW_BEFORE = 30 * 24 * 3600


def new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_b64(key: Ed25519PrivateKey) -> str:
    return b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    """Load a raw 32-byte private key, or create one with mode 0600."""
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = new_key()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return key


def sign(key: Ed25519PrivateKey, message: bytes) -> str:
    return b64(key.sign(message))


def verify(public: str, message: bytes, signature: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(unb64(public)).verify(unb64(signature), message)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---- handshakes


def handshake_message(nonce: str, instance: str = "") -> bytes:
    """Tunnel: what each side signs (the other side's nonce and, for the computer, its own public key)."""
    return f"{HANDSHAKE_CONTEXT}|{nonce}|{instance}".encode()


def device_message(nonce: str, device: str) -> bytes:
    """Device connection: what the device signs with its auth key (SPEC §6.3)."""
    return f"{DEVICE_CONTEXT}|{nonce}|{device}".encode()


# ---- binding certificates (routing only, SPEC §6.4)


def gateway_proof_message(device_nonce: str) -> bytes:
    """What the gateway signs to prove to a device that it holds the pinned key (SPEC §6.3)."""
    return f"{GATEWAY_PROOF_CONTEXT}|{device_nonce}".encode()


def cert_body(cert: Cert) -> bytes:
    fields = {
        "v": cert.v,
        "device": cert.device,
        "instance": cert.instance,
        "tag": cert.tag,
        "issued": cert.issued,
        "expires": cert.expires,
    }
    return ("aha-cert-v2|" + json.dumps(fields, sort_keys=True, separators=(",", ":"))).encode()


def issue_cert(gateway_key: Ed25519PrivateKey, device: str, instance: str, tag: str, now: int | None = None) -> Cert:
    now = int(time.time()) if now is None else now
    unsigned = Cert(device=device, instance=instance, tag=tag, issued=now, expires=now + CERT_LIFETIME)
    return unsigned.model_copy(update={"sig": sign(gateway_key, cert_body(unsigned))})


def cert_valid(cert: Cert, gateway_public: str, instance: str | None = None, now: int | None = None) -> bool:
    """Signed by this gateway, not expired, and (if given) for this instance."""
    now = int(time.time()) if now is None else now
    return (
        (instance is None or cert.instance == instance)
        and cert.issued <= now + 300
        and now < cert.expires
        and verify(gateway_public, cert_body(cert), cert.sig)
    )


def needs_renewal(cert: Cert, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    return cert.expires - now < RENEW_BEFORE
