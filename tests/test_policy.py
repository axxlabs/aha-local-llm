"""SPEC §5.2: every row of the browser policy, from the real arguments."""

import pytest

from aha_local.policy.browser import decide, form_for, offered
from aha_local.policy.urls import check_url, site_of


@pytest.mark.parametrize(
    "name,args,outcome",
    [
        ("browser_type", {"text": "Felipe"}, "approval"),
        ("browser_fill_form", {"fields": []}, "approval"),
        ("browser_select_option", {"values": ["2"]}, "approval"),
        ("browser_press_key", {"key": "a"}, "approval"),
        ("browser_press_key", {"key": "Control+V"}, "approval"),
        ("browser_press_key", {"key": "Enter"}, "auto"),
        ("browser_press_key", {"key": "Tab"}, "auto"),
        ("browser_handle_dialog", {"accept": True, "promptText": "hi"}, "approval"),
        ("browser_handle_dialog", {"accept": True}, "auto"),
        ("browser_click", {"target": "e12"}, "auto"),
        ("browser_mouse_click_xy", {"x": 1, "y": 2}, "auto"),
        ("browser_snapshot", {}, "auto"),
        ("browser_take_screenshot", {}, "auto"),
        ("browser_tabs", {"action": "list"}, "auto"),
        ("browser_evaluate", {"function": "() => 1"}, "block"),
        ("browser_run_code_unsafe", {"code": "x"}, "block"),
        ("browser_file_upload", {"paths": ["/etc/passwd"]}, "block"),
        ("browser_network_requests", {}, "block"),
        ("browser_something_new", {}, "approval"),
    ],
)
def test_browser_policy(name, args, outcome):
    assert decide(name, args, allow_all_sites=True, approved_sites=set()).outcome == outcome


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
    assert decide("browser_navigate", {"url": url}, True, set()).outcome == "block"
    assert check_url(url)[1]


def test_new_sites_follow_the_setting():
    assert decide("browser_navigate", {"url": "https://thefork.co.uk/x"}, True, set()).outcome == "auto"
    v = decide("browser_navigate", {"url": "https://www.thefork.co.uk/x"}, False, set())
    assert v.outcome == "approval" and v.site == "thefork.co.uk"
    assert decide("browser_navigate", {"url": "https://thefork.co.uk/y"}, False, {"thefork.co.uk"}).outcome == "auto"
    assert decide("browser_tabs", {"action": "new", "url": "http://10.1.1.1"}, True, set()).outcome == "block"


def test_hidden_tools_are_not_offered():
    assert not offered("browser_evaluate") and not offered("browser_resize")
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
