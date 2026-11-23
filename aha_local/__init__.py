# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""aha-local-llm: aha! A Truly Private Personal Assistant, on your own computer."""

import os
import subprocess
from pathlib import Path

# SPEC §10.2: everything is "hot-staging" for now, named by the commit it was built from. Once releases start,
# RELEASE becomes an incremental release number.
RELEASE = "hot-staging"


def _build() -> str:
    """The commit: from the image build (AHA_BUILD), or the source checkout when run from one."""
    if build := os.environ.get("AHA_BUILD", "").strip():
        return build[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


BUILD = _build()
__version__ = f"{RELEASE} ({BUILD})"
