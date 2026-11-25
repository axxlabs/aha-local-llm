# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The person's own actions: the same from Settings and from a chat command, each audited (SPEC §5.4, §5.8)."""

import json

from aha_local import actions
from aha_local.tools import logins, memory


def audited(account) -> list[dict]:
    return [json.loads(r) for r in account.vault.records(account.dir / "audit.jsonl")]


def test_settings_and_chat_forget_the_same_way_and_both_are_audited(accounts):
    fe = accounts.create("Test Person")
    for via in ("settings", "chat"):
        logins.save(fe, "www.waitrose.com", "test@example.com", "not-a-real-password")
        assert actions.forget_login(fe, "https://www.waitrose.com/account", via)
        assert not logins.sites(fe)
    assert not actions.forget_login(fe, "waitrose.com", "chat")  # nothing left: nothing audited
    fact = memory.add_fact(fe, "Prefers window seats")
    assert actions.forget_memory(fe, fact["id"], "settings") and actions.memories(fe) == []
    assert actions.set_trust(fe, "www.waitrose.com", True, "settings")
    assert not actions.set_trust(fe, "192.168.1.1", True, "settings")
    lines = [{k: v for k, v in e.items() if k != "ts"} for e in audited(fe)]
    assert {"login_forgotten": "waitrose.com", "via": "settings"} in lines
    assert {"login_forgotten": "waitrose.com", "via": "chat"} in lines
    assert {"memory_forgotten": fact["id"], "via": "settings"} in lines
    assert {"trusted_site": "waitrose.com", "via": "settings"} in lines
    assert "not-a-real-password" not in json.dumps(lines)


def test_a_topic_entry_is_forgotten_like_a_fact(accounts):
    import time

    from aha_local import context

    fe = accounts.create("Test Person")
    context.save_topic(fe, 7, time.time(), "Evri parcel", "Tracking H01ABC123")
    assert [t["title"] for t in actions.topics(fe)] == ["Evri parcel"]
    assert actions.forget_memory(fe, "t7", "settings") and actions.topics(fe) == []
    assert {"memory_forgotten": "t7", "via": "settings"} in [
        {k: v for k, v in e.items() if k != "ts"} for e in audited(fe)
    ]
