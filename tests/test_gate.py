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
    for kind in (
        "email",
        "draft",
        "calendar",
        "calendar_delete",
        "schedule",
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


def test_a_made_up_address_is_flagged_on_its_card():
    from aha_local.policy import addresses
    from aha_local.policy.browser import MADE_UP, open_form

    seen: set[tuple] = set()
    addresses.remember(seen, "Cancel (https://www.sevenrooms.com/direct/cancel-reservation/ahNzfX?lang=en)")
    guessed = "https://www.sevenrooms.com/management/NZ5JPFYU9X7"
    assert addresses.made_up(guessed, seen)
    assert not addresses.made_up("https://www.sevenrooms.com/direct/cancel-reservation/ahNzfX?lang=en", seen)
    assert not addresses.made_up("https://www.sevenrooms.com/", seen)  # a home page is fine
    assert not addresses.made_up("https://www.google.com/search?q=flat+iron", seen)  # a search
    fields = open_form(guessed, made_up=True).fields
    assert ("Note", MADE_UP, "info") in [(f.label, f.value, f.kind) for f in fields]
