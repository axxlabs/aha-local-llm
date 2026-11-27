# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""SPEC §5.2: every row of the browser policy, from the real arguments."""

import json
import re
from pathlib import Path

import pytest

from aha_local.policy.addresses import key
from aha_local.policy.browser import classify, form_for, offered
from aha_local.policy.gate import Action, Facts, decide
from aha_local.policy.urls import check_url, site_of


def gate(name, args, page="shop.example", **facts):
    return decide(classify(name, args, page), Facts(**facts)).outcome


@pytest.mark.parametrize(
    "name,args,outcome",
    [
        ("browser_type", {"text": "Felipe"}, "card"),
        ("browser_fill_form", {"fields": []}, "card"),
        ("browser_select_option", {"values": ["2"]}, "card"),
        ("browser_press_key", {"key": "a"}, "card"),
        ("browser_press_key", {"key": "Control+V"}, "card"),
        ("browser_press_key", {"key": "Enter"}, "run"),
        ("browser_press_key", {"key": "Tab"}, "run"),
        ("browser_handle_dialog", {"accept": True, "promptText": "hi"}, "card"),
        ("browser_handle_dialog", {"accept": True}, "run"),
        ("browser_click", {"target": "e12"}, "run"),
        ("browser_mouse_click_xy", {"x": 1, "y": 2}, "run"),
        ("browser_snapshot", {}, "run"),
        ("browser_take_screenshot", {}, "run"),
        ("browser_tabs", {"action": "list"}, "run"),
        ("browser_evaluate", {"function": "() => 1"}, "block"),
        ("browser_run_code_unsafe", {"code": "x"}, "block"),
        ("browser_file_upload", {"paths": ["/etc/passwd"]}, "block"),
        ("browser_network_requests", {}, "block"),
        ("browser_something_new", {}, "card"),
    ],
)
def test_browser_actions(name, args, outcome):
    assert gate(name, args) == outcome


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "http://192.168.1.1/admin",
        "http://10.0.0.5",
        "http://[::1]/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://router.lan",
        "http://printer.local",
        "http://intranet",
        "http://u:p@example.com",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_local_and_odd_urls_are_blocked(url):
    assert gate("browser_navigate", {"url": url}) == "block"
    assert gate("browser_navigate", {"url": url}, trusted={"127.0.0.1", "example.com"}) == "block"
    assert check_url(url)[1]


def nav(url):
    return ("browser_navigate", {"url": url})


SHOWN = {key("https://evil.example/?d=1")}
TRUSTED = {"waitrose.com"}


# SPEC §5.2: kind × trusted / opened / shown. One row per rule.
@pytest.mark.made_up_rule
@pytest.mark.parametrize(
    "call,facts,outcome",
    [
        # 3. typing on a trusted site runs
        (("browser_type", {"text": "x"}), {"trusted": TRUSTED}, "run"),
        # 5. typing elsewhere is a card
        (("browser_type", {"text": "x"}), {"trusted": {"other.example"}}, "card"),
        # 7. open: a shown address runs, a home page asks once per site, anything else is refused
        (nav("https://evil.example/?d=1"), {"shown": SHOWN}, "run"),
        (nav("https://evil.example/?d=1"), {}, "refuse"),
        (nav("https://evil.example/?d=1"), {"opened": {"evil.example"}}, "refuse"),
        (nav("https://evil.example/#a=b"), {}, "refuse"),
        (nav("https://www.waitrose.com/x"), {"trusted": TRUSTED}, "refuse"),  # trust is no licence to guess
        (nav("https://thefork.co.uk/"), {}, "card"),
        (nav("https://thefork.co.uk/"), {"opened": {"thefork.co.uk"}}, "run"),
        (nav("https://www.waitrose.com/"), {"trusted": TRUSTED}, "run"),
        (("browser_tabs", {"action": "new", "url": "https://evil.example/?d=1"}), {}, "refuse"),
        # 6. a search engine's results page: web_search does searches
        (nav("https://www.google.com/search?q=lisbon"), {}, "refuse"),
        (nav("https://www.google.com/search?q=lisbon"), {"trusted": {"google.com"}}, "refuse"),
        (nav("https://www.google.com/"), {}, "card"),  # a home page is an open
        (nav("https://docs.google.com/forms/d/x/formResponse?entry.1=secret"), {}, "refuse"),  # not a search
    ],
)
def test_the_gate(call, facts, outcome):
    assert gate(*call, page="waitrose.com" if call[0] == "browser_type" else "", **facts) == outcome


def test_send_is_always_a_card():
    """SPEC §5.2 rule 2: email, memories, background tasks, MCP writes; trust doesn't apply."""
    for facts in ({}, {"trusted": {"gmail.com", ""}}):
        assert decide(Action("send"), Facts(**facts)).outcome == "card"


def test_trusted_runs_are_marked_for_the_audit():
    assert decide(classify("browser_type", {}, "waitrose.com"), Facts(trusted=TRUSTED)).why == "trusted"
    assert decide(classify(*nav("https://www.waitrose.com/")), Facts(trusted=TRUSTED)).why == "trusted"
    assert decide(classify(*nav("https://thefork.co.uk/")), Facts()).why == "open"


def test_hidden_tools_are_not_offered():
    assert not offered("browser_evaluate") and not offered("browser_resize")
    assert not offered("browser_close")  # it drops everything the browser remembers (the Waitrose sign-out)
    assert offered("browser_click") and offered("browser_type")


def test_site_of():
    assert site_of("www.flatironsteak.co.uk") == "flatironsteak.co.uk"
    assert site_of("book.thefork.com") == "thefork.com"


def test_forms_carry_the_real_values_and_edits_land_in_the_arguments():
    args = {
        "fields": [
            {"name": "First name", "type": "textbox", "target": "e1", "value": "Felipe"},
            {"name": "Phone", "type": "textbox", "target": "e2", "value": "07700 900123"},
        ]
    }
    form = form_for("browser_fill_form", args, "thefork.co.uk")
    assert form.title == "Fill in 2 fields on thefork.co.uk?"
    assert [f.value for f in form.fields] == ["Felipe", "07700 900123"]
    final = form.apply("browser_fill_form", args, ["Fe", "07700 900456"])
    assert [f["value"] for f in final["fields"]] == ["Fe", "07700 900456"]
    assert final["fields"][1]["target"] == "e2"
    typed = form_for("browser_type", {"element": "Search", "text": "Lisbon"}, "")
    assert typed.apply("browser_type", {"element": "Search", "text": "Lisbon", "submit": True}, ["Porto"]) == {
        "element": "Search",
        "text": "Porto",
        "submit": True,
    }


def test_targets_are_normalised_and_labels_come_from_the_page():
    from aha_local.tools.browser_page import element_label, normalise_targets

    assert normalise_targets({"target": "[ref=e23]", "text": "x"}) == {"target": "e23", "text": "x"}
    assert normalise_targets({"fields": [{"target": "ref=e5", "value": "a"}]})["fields"][0]["target"] == "e5"
    snap = (
        '- banner [ref=e4]:\n  - searchbox "Search Wikipedia" [ref=e23]\n  - button "Search" [ref=e24] [cursor=pointer]'
    )
    assert element_label(snap, "e23") == "Search Wikipedia"
    assert element_label(snap, "e24") == "Search"
    assert element_label('- textbox "Customer name:" [ref=e7]', "e7") == "Customer name"
    assert element_label(snap, "e99") == ""


def test_label_found_in_a_snapshot_cut_to_fit():
    from aha_local.tools.browser_page import YAML_BLOCK, element_label

    text = '### Snapshot\n```yaml\n- searchbox "Search Wikipedia" [ref=e23]\n- link "x" [ref=e24]\n[… snapshot cut'
    assert element_label(YAML_BLOCK.search(text).group(1), "e23") == "Search Wikipedia"


AUDIT_WORDS = re.compile(
    r"\bapprov|(?<!payment )\bcards?\b(?! (?:details|guarantee|or id))|\btrust(?:ed|s)?\b(?! more)", re.I
)


def test_the_model_never_hears_about_cards_or_trust():
    """SPEC §5.1: prompts and tool descriptions say nothing of cards, approvals or trust."""

    root = Path(__file__).parents[1] / "aha_local"
    files = [*(root / "prompts").rglob("*.md"), *(root / "skills").glob("*.md")]
    assert len(files) > 10
    for f in files:
        found = AUDIT_WORDS.findall(f.read_text())
        assert not found, (f.name, found)
    from aha_local.tools import background, calendar, guard, mail, memory, registry, web
    from aha_local.tools.browser import HARNESS_TOOLS

    reg = registry.Registry()
    for m in (background, calendar, mail, memory, web):
        m.register(reg)
    for t in HARNESS_TOOLS:
        reg.add(t)
    for spec in reg.specs():
        text = json.dumps(spec)
        assert not AUDIT_WORDS.search(text), spec["function"]["name"]
    assert not AUDIT_WORDS.search(guard.STOPPED)


def test_typing_into_a_message_box_is_a_send_on_any_site():
    """SPEC §5.2 rule 2: told by the field's own label on the page, not by a list of sites (seen: LinkedIn's
    messages went unsigned)."""
    from aha_local.policy.browser import sends_message

    for labels in (
        ["Type a message"],
        ["Write a message…"],
        ["Reply"],
        ["Your response"],
        ["Add a comment"],
        ["Digite uma mensagem"],
        ["Escribe un mensaje"],
        ["Name", "Your message"],  # a form with a message in it
    ):
        assert sends_message("browser_type", labels), labels
    for labels in (["Search messages"], ["Pesquisar ou começar uma nova conversa"], ["Postcode"], [""], []):
        assert not sends_message("browser_type", labels), labels
    assert not sends_message("browser_press_key", ["Write a message…"])  # a key alone is typing, not a message


def test_chats_are_user_driven():
    """SPEC §5.2 "Chats are user-driven": a message on any site, and anything typed on a chat site, is a card every
    time, trusted or not, with no "always"; the open chat's name is cleaned to one short printable line."""
    from aha_local.policy import chats
    from aha_local.policy.gate import Action, Facts, decide
    from aha_local.tools import guard

    trusted = Facts(trusted={"whatsapp.com", "linkedin.com"})
    for action in (Action("send", "linkedin.com"), Action("send", "whatsapp.com"), Action("type", "whatsapp.com")):
        assert decide(action, trusted).outcome == "card" and guard.allow_key(action) == ""
    # "always" on a chat message covers the chat the page named, and only that one
    sam = guard.allow_key(Action("send", "whatsapp.com", chat="Sam"))
    assert sam == "message:whatsapp.com:Sam" != guard.allow_key(Action("send", "whatsapp.com", chat="Ana"))
    assert guard.allow_label(sam) == "Send messages to Sam on whatsapp.com"
    assert decide(Action("type", "linkedin.com"), trusted).outcome == "run"  # not a chat site: trust still counts
    assert guard.allow_key(Action("send", what="email", to=("sam@example.com",))) == "email:sam@example.com"
    assert guard.allow_key(Action("send", what="task")) == ""  # starting a background task: no "always" either
    assert (
        chats.clean("  Sam‮ \n Smith ") == "Sam Smith" and chats.clean(None) == "" and len(chats.clean("x" * 500)) == 80
    )


def test_the_open_chat_is_checked_against_the_message_box():
    """policy/chats.resolve: the header's name, checked against the box's "Type a message to …" (real labels)."""
    from aha_local.policy.chats import resolve

    box = ["Type a message to Haibo E"]
    assert resolve("whatsapp.com", "Haibo E", box) == "Haibo E"
    assert resolve("whatsapp.com", "Natalia Lavratti", box) == ""  # they disagree: nothing is typed
    assert resolve("whatsapp.com", "", box) == "Haibo E"  # the header unreadable: the box alone
    assert resolve("whatsapp.com", "Meta AI", ["Message"]) == "Meta AI"  # a box that names no one
    assert resolve("whatsapp.com", "", ["Message"]) == ""
