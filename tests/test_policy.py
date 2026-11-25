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


# SPEC §5.2: kind × trusted / private / shown / data in the address / allow_all_sites. One row per rule.
@pytest.mark.parametrize(
    "call,facts,outcome",
    [
        # 3. a trusted destination runs, whatever else holds
        (("browser_type", {"text": "x"}), {"trusted": TRUSTED}, "run"),
        (("browser_type", {"text": "x"}), {"trusted": TRUSTED, "private": True}, "run"),
        (nav("https://www.waitrose.com/search?q=fluid"), {"trusted": TRUSTED}, "run"),  # data in it: the bug
        (nav("https://www.waitrose.com/search?q=fluid"), {"trusted": TRUSTED, "private": True}, "run"),
        (nav("https://www.waitrose.com/x"), {"trusted": TRUSTED, "allow_all_sites": False}, "run"),
        # 5. typing elsewhere is a card
        (("browser_type", {"text": "x"}), {"trusted": {"other.example"}}, "card"),
        # 7. open
        (nav("https://thefork.co.uk/x"), {}, "run"),
        (nav("https://thefork.co.uk/x"), {"allow_all_sites": False}, "card"),
        (nav("https://thefork.co.uk/x"), {"allow_all_sites": False, "opened": {"thefork.co.uk"}}, "run"),
        (nav("https://evil.example/?d=1"), {}, "card"),  # the model wrote data into it
        (nav("https://evil.example/?d=1"), {"shown": SHOWN}, "run"),  # a link the page showed
        (nav("https://evil.example/?d=1"), {"opened": {"evil.example"}}, "card"),
        (nav("https://evil.example/"), {"private": True}, "card"),  # after mail or a signed-in page
        (nav("https://evil.example/"), {"private": True, "opened": {"evil.example"}}, "run"),
        (nav("https://evil.example/#a=b"), {}, "card"),
        # 6. search
        (nav("https://www.google.com/search?q=lisbon"), {}, "run"),
        (nav("https://www.google.com/search?q=lisbon"), {"private": True}, "card"),
        (nav("https://docs.google.com/forms/d/x/formResponse?entry.1=secret"), {}, "card"),  # not a search
        (("browser_tabs", {"action": "new", "url": "https://evil.example/?d=1"}), {}, "card"),
    ],
)
def test_the_gate(call, facts, outcome):
    assert gate(*call, page="waitrose.com" if call[0] == "browser_type" else "", **facts) == outcome


def test_send_is_always_a_card():
    """SPEC §5.2 rule 2: email, memories, background tasks, MCP writes; trust doesn't apply."""
    for facts in ({}, {"trusted": {"gmail.com", ""}}, {"private": True}):
        assert decide(Action("send"), Facts(**facts)).outcome == "card"
    assert decide(Action("search"), Facts()).outcome == "run"
    assert decide(Action("search"), Facts(private=True)).outcome == "card"


def test_trusted_runs_are_marked_for_the_audit():
    assert decide(classify("browser_type", {}, "waitrose.com"), Facts(trusted=TRUSTED)).why == "trusted"
    assert decide(classify(*nav("https://evil.example/?d=1")), Facts()).why == "data"


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

    prompts = Path(__file__).parents[1] / "aha_local" / "prompts"
    for f in prompts.glob("*.md"):
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


async def test_private_data_is_declared_on_the_tool_and_set_by_the_registry():
    """SPEC §5.2: no tool sets it; a new one that reads private data only has to say so where it's registered."""
    from types import SimpleNamespace

    from aha_local.tools.registry import Registry, TaskState, Tool, ToolResult, params

    async def ok(args, ctx):
        return ToolResult("their data")

    async def failed(args, ctx):
        return ToolResult("not connected", error=True)

    reg = Registry()
    reg.add(Tool("reads_private", "", params(), ok, private=True))
    reg.add(Tool("reads_private_but_fails", "", params(), failed, private=True))
    reg.add(Tool("reads_public", "", params(), ok))
    ctx = SimpleNamespace(task=TaskState())
    await reg.call("reads_public", {}, ctx)
    await reg.call("reads_private_but_fails", {}, ctx)
    assert not ctx.task.private
    await reg.call("reads_private", {}, ctx)
    assert ctx.task.private
    from aha_local.tools import calendar, mail

    for module in (mail, calendar):
        module.register(reg)
    assert {t for t in reg.names() if reg._tools[t].private} == {
        "email_search",
        "email_read",
        "calendar_list",
        "calendar_update",
        "calendar_delete",
        "reads_private",
        "reads_private_but_fails",
    }


def test_on_a_messaging_site_typing_is_a_send_unless_clearly_the_search_box():
    """SPEC §5.2: on the safe side, so a renamed or translated message box is still a card."""
    from aha_local.policy.browser import sends_message

    for labels in (["Type a message"], ["Digite uma mensagem"], ["Composer"], [""], []):
        assert sends_message("browser_type", "whatsapp.com", labels), labels
    for labels in (["Search input textbox"], ["Pesquisar ou começar uma nova conversa"]):
        assert not sends_message("browser_type", "whatsapp.com", labels), labels
    assert sends_message("browser_press_key", "whatsapp.com", [])  # a key goes wherever the focus is
    assert not sends_message("browser_type", "waitrose.com", ["Type a message"])  # not a messaging site
