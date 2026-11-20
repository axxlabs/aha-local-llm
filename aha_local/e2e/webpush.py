# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Web Push payload encryption (RFC 8291, aes128gcm) on the computer (SPEC §6.6).

The payload inside is already sealed with the device's push key (AHA-PQ), so neither the gateway (which adds
VAPID and POSTs it) nor the push service can read or forge it.
"""

from __future__ import annotations

import base64
import re

import http_ece
from cryptography.hazmat.primitives.asymmetric import ec

ENDPOINT = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/\S{1,900}$")


def _b64url_d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def valid_subscription(sub: object) -> bool:
    if not isinstance(sub, dict) or not isinstance(sub.get("endpoint"), str) or not ENDPOINT.match(sub["endpoint"]):
        return False
    keys = sub.get("keys")
    if not isinstance(keys, dict):
        return False
    try:
        return len(_b64url_d(keys["p256dh"])) == 65 and len(_b64url_d(keys["auth"])) == 16
    except (KeyError, TypeError, ValueError):
        return False


def encrypt(subscription: dict, payload: bytes) -> bytes:
    return http_ece.encrypt(
        payload,
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=_b64url_d(subscription["keys"]["p256dh"]),
        auth_secret=_b64url_d(subscription["keys"]["auth"]),
        version="aes128gcm",
    )
