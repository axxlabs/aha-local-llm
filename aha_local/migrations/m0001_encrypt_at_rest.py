# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Encryption at rest (SPEC §3.8.1): every plaintext file of an earlier version, encrypted in place."""

from __future__ import annotations

import json


def needed(vault) -> bool:
    marker = vault.root / "format.json"
    return not (marker.exists() and json.loads(marker.read_text()).get("rest") == 1)


def run(vault) -> None:
    vault.encrypt_plaintext()
