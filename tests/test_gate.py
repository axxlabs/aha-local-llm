# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
import pytest

from aha_local.policy.gate import Action, Facts, decide


def test_every_card_says_why_in_code():
    """The (i) on a card: the rule that asked, decided and worded by code (SPEC §5.2, §7.4)."""
    from aha_local import approvals
    from aha_local.policy.gate import WHY, explain

    plain = Facts()
    cases = [
        (Action("send", "whatsapp.com"), plain, "send"),
        (Action("type", "waitrose.com"), plain, "type"),
        (Action("search", "google.com", "https://www.google.com/search?q=x"), plain, "search"),
        (Action("open", "x.com", "https://x.com/"), plain, "open"),
        (Action("other", "x.com"), plain, "other"),
    ]
    for action, facts, why in cases:
        verdict = decide(action, facts)
        assert (verdict.outcome, verdict.why) == ("card", why), action
        assert "{site}" not in explain(why, action.site)
    assert set(WHY) == {why for _, _, why in cases}
    assert "waitrose.com isn't one of your trusted sites" in explain("type", "waitrose.com")
    assert explain("trusted") == ""  # a run: no card, no reason
    for kind in (
        "email",
        "draft",
        "calendar",
        "calendar_delete",
        "task",
        "memory",
        "forget",
        "signin",
        "check",
    ):
        assert approvals.KINDS[kind].why and approvals.KINDS[kind].icon


def test_an_html_emails_links_keep_their_addresses():
    """Seen: a cancel button in an HTML-only email lost its address, and the model made one up."""
    from aha_local.tools.mail import _html_text

    html = (
        '<p>Need to change plans? <a class="btn" href="https://www.sevenrooms.com/direct/cancel-reservation/ah'
        'NzfX?lang=en&amp;x=1"><span>Cancel reservation</span></a></p><p><a href="mailto:a@b.com">Email us</a></p>'
    )
    text = _html_text(html)
    assert "Cancel reservation (https://www.sevenrooms.com/direct/cancel-reservation/ahNzfX?lang=en&x=1)" in text
    assert "Email us" in text and "mailto" not in text


@pytest.mark.made_up_rule
def test_what_counts_as_a_made_up_address():
    from aha_local.policy import addresses

    seen: set[tuple] = set()
    addresses.remember(seen, "Cancel (https://www.sevenrooms.com/direct/cancel-reservation/ahNzfX?lang=en)")
    guessed = "https://www.sevenrooms.com/management/NZ5JPFYU9X7"
    assert addresses.made_up(guessed, seen)
    assert not addresses.made_up("https://www.sevenrooms.com/direct/cancel-reservation/ahNzfX?lang=en", seen)
    assert not addresses.made_up("https://www.sevenrooms.com/", seen)  # a home page is fine
    assert addresses.made_up("https://www.google.com/search?q=flat+iron", seen)  # web_search does searches
    assert addresses.home(guessed) == "https://www.sevenrooms.com/"


def test_a_search_is_its_own_rule_not_a_trusted_sites():
    """Trusting google.com is for typing there; a search still shows its words first."""
    from aha_local.policy.gate import Action, Facts, decide

    trusting = Facts(trusted={"google.com"})
    assert decide(Action("search", "google.com", "https://www.google.com/search?q=x"), trusting).outcome == "card"
    assert decide(Action("search"), trusting).outcome == "card"
    assert decide(Action("type", "google.com"), trusting).outcome == "run"
