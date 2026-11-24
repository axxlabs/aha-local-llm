# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
from aha_local.policy.gate import Action, Facts, decide


def test_every_card_says_why_in_code():
    """The (i) on a card: the rule that asked, decided and worded by code (SPEC §5.2, §7.4)."""
    from aha_local import approvals
    from aha_local.policy.gate import WHY, explain

    plain, private = Facts(), Facts(private=True)
    cases = [
        (Action("send", "whatsapp.com"), plain, "send"),
        (Action("type", "waitrose.com"), plain, "type"),
        (Action("type", "waitrose.com"), private, "type_private"),
        (Action("search", "google.com", "https://www.google.com/search?q=x"), private, "search_private"),
        (Action("open", "x.com", "https://x.com/"), private, "open_private"),
        (Action("open", "x.com", "https://x.com/?email=a@b.com"), plain, "data"),
        (Action("open", "x.com", "https://x.com/?email=a@b.com"), private, "data_private"),
        (Action("open", "x.com", "https://x.com/"), Facts(allow_all_sites=False), "new_site"),
        (Action("other", "x.com"), plain, "other"),
    ]
    for action, facts, why in cases:
        verdict = decide(action, facts)
        assert (verdict.outcome, verdict.why) == ("card", why), action
        assert "{site}" not in explain(why, action.site)
    assert set(WHY) == {why for _, _, why in cases}
    assert "waitrose.com isn't one of your trusted sites" in explain("type", "waitrose.com")
    assert "read your email" in explain("type_private", "waitrose.com")
    assert explain("trusted") == ""  # a run: no card, no reason
    for kind in ("email", "draft", "calendar", "calendar_delete", "schedule", "memory", "forget", "signin", "check"):
        assert approvals.KIND_WHY[kind]
