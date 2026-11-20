"""Ed25519 keys, binding certificates and handshake signatures (SPEC §6.1, §6.2, §6.4).

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

from .protocol import HANDSHAKE_CONTEXT, Cert, b64, unb64

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


# ---- handshake


def handshake_message(nonce: str, instance: str = "") -> bytes:
    """What each side signs: the other side's nonce (and, for the computer, its own public key)."""
    return f"{HANDSHAKE_CONTEXT}|{nonce}|{instance}".encode()


# ---- binding certificates


def cert_body(cert: Cert) -> bytes:
    fields = {
        "v": cert.v,
        "address": cert.address,
        "instance": cert.instance,
        "issued": cert.issued,
        "expires": cert.expires,
    }
    return ("aha-cert-v1|" + json.dumps(fields, sort_keys=True, separators=(",", ":"))).encode()


def issue_cert(gateway_key: Ed25519PrivateKey, address: str, instance: str, now: int | None = None) -> Cert:
    now = int(time.time()) if now is None else now
    unsigned = Cert(address=address, instance=instance, issued=now, expires=now + CERT_LIFETIME)
    return unsigned.model_copy(update={"sig": sign(gateway_key, cert_body(unsigned))})


def cert_valid(cert: Cert, gateway_public: str, instance: str, now: int | None = None) -> bool:
    """Signed by this gateway, for this instance, and not expired."""
    now = int(time.time()) if now is None else now
    return (
        cert.instance == instance
        and cert.issued <= now + 300
        and now < cert.expires
        and verify(gateway_public, cert_body(cert), cert.sig)
    )


def needs_renewal(cert: Cert, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    return cert.expires - now < RENEW_BEFORE
