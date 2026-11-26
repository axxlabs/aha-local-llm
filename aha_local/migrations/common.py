# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""What the migration scripts share."""

from __future__ import annotations

from pathlib import Path


def accounts(vault) -> list[Path]:
    """Each account's folder."""
    root = vault.root / "accounts"
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
