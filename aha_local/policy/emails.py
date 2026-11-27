# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Email addresses in a card's fields: who an email or an event goes to (what an "always" tick covers, SPEC §5.3)."""

from __future__ import annotations

import re

EMAIL = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[a-z]{2,63}", re.I)


def emails_in(text: str) -> list[str]:
    """Every address in a To, Cc or guests field, lower-cased."""
    return [m.lower() for m in EMAIL.findall(text or "")]
