# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The messages between the app and the computer (SPEC §6.5), checked against the file both repos share."""

import json
import re
from pathlib import Path

import pytest

from aha_local.e2e.channel import HANDLERS

HERE = Path(__file__).resolve().parent
SCHEMA = json.loads((HERE / "vectors" / "device_messages.json").read_text())
SRC = HERE.parent / "aha_local"


def test_every_device_message_has_one_handler():
    assert set(HANDLERS) == set(SCHEMA["device"])


def test_the_computer_sends_only_listed_messages():
    sent = {m for p in SRC.rglob("*.py") for m in re.findall(r'"t": "([a-z_]+)"', p.read_text())}
    assert sent <= set(SCHEMA["computer"]) | set(SCHEMA["pairing"]["computer"]), sent - set(SCHEMA["computer"])


def test_the_apps_copy_is_the_same():
    app = HERE.parents[1] / "aha-app" / "test" / "vectors" / "device_messages.json"
    if not app.exists():
        pytest.skip("aha-app not checked out next to aha-local-llm")
    assert json.loads(app.read_text()) == SCHEMA
