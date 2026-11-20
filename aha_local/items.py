# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Small value types shared by tools, approvals and the channel."""

from __future__ import annotations

from dataclasses import dataclass

IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")


@dataclass(frozen=True)
class Image:
    mime: str
    b64: str

    def to_dict(self) -> dict:
        return {"mime": self.mime, "b64": self.b64}
