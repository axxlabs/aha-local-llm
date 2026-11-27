# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topic numbers (SPEC §7.4 "Topic numbers", 2026-11-27: "add numbering to the topics. So I can ask the model to
refer to a topic by number").

Every titled part of the chat gets a number when its first title is given: a topic of the conversation, and a
background task's note, which is a part of its own. 1, 2, 3… per person, never reused. `numbers.json` is the one
place they're kept, `{"next": N, "parts": {"<seq of the part's first item>": n}}`: a `title` item carries its
part's number when it's published and whenever the app is sent it again (`Account.items`), never stored in it.
"""

from __future__ import annotations

FILE = "numbers.json"


def load(account) -> dict:
    data = account.vault.read_json(account.dir / FILE, None) or {}
    return {"next": int(data.get("next", 1)), "parts": {str(k): int(v) for k, v in (data.get("parts") or {}).items()}}


def part(account, start: int) -> int:
    """The number of the part that starts at item `start`, given now if it has none."""
    data = load(account)
    key = str(start)
    if key not in data["parts"]:
        data["parts"][key] = data["next"]
        data["next"] += 1
        account.vault.write_json(account.dir / FILE, data)
    return data["parts"][key]


def parts(account) -> dict[int, int]:
    """Each numbered part's first item → its number."""
    return {int(k): v for k, v in load(account)["parts"].items()}


def start_of(account, n: int) -> int | None:
    return next((s for s, m in parts(account).items() if m == n), None)
