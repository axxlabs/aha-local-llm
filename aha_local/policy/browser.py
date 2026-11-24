# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Browser tool calls as actions for the gate (SPEC §5.2), and the cards that show them (§5.3).

Decided from the real arguments of each call, never from the model's descriptions. Clicks and reading can't
carry new information out of the computer; typing and addresses can.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..approvals import Field
from . import addresses
from .gate import Action
from .urls import check_url

BLOCK = {
    "browser_evaluate",  # arbitrary JavaScript
    "browser_run_code_unsafe",  # arbitrary Playwright code
    "browser_file_upload",  # local files
}
HIDE = {  # not offered: rarely useful, and every definition costs prompt tokens on every request
    "browser_console_messages",
    "browser_network_requests",
    "browser_network_request",
    "browser_emulate_media",
    "browser_resize",
    "browser_drag",
    "browser_drop",
    "browser_mouse_move_xy",
    "browser_mouse_drag_xy",
    "browser_mouse_down",
    "browser_mouse_up",
    # Closing the browser drops the context with everything it remembers: the next call opens a blank one, and
    # saving that after the task would sign the person out everywhere. The harness closes it when idle.
    "browser_close",
}
AUTO = {
    "browser_click",
    "browser_hover",
    "browser_navigate_back",
    "browser_mouse_click_xy",
    "browser_mouse_wheel",
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_find",
    "browser_wait_for",
}
SAFE_KEYS = {
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Shift+Tab",
}


# Messaging sites (SPEC §5.2 rule 2): typing into the message box sends a message to someone, so it's a `send`,
# a card every time, trusted or not; typing elsewhere there (the search box) follows the usual rules. Their pages
# are the person's own messages, so a page of one makes the task hold private data.
MESSAGE_SITES = {"whatsapp.com"}
MESSAGE_BOX = re.compile(r"\btype a message\b|^message\b|\bmensagem\b|\bescreva\b", re.I)
SEARCH_BOX = re.compile(r"\bsearch\b|\bpesquis|\bbusca|\bbuscar\b|\bsuche\b|\brecherch", re.I)


def sends_message(name: str, site: str, labels: list[str]) -> bool:
    """Typing on a messaging site sends a message, unless it's clearly into the search box: decided from the page's
    own labels, on the safe side (a renamed or translated message box is still a send). A typed key goes wherever
    the focus is: a send too."""
    if site not in MESSAGE_SITES:
        return False
    if name == "browser_press_key":
        return True
    return not labels or not all(label and SEARCH_BOX.search(label) for label in labels)


def offered(name: str) -> bool:
    return name not in BLOCK and name not in HIDE


TYPING_KINDS = {"browser_type", "browser_fill_form", "browser_select_option"}


def classify(name: str, args: dict, page_site: str = "") -> Action:
    """A browser tool call as an action for the gate (SPEC §5.2). `page_site` is the current page's site, read
    from the browser's own address, never from the model."""
    if name in BLOCK or name in HIDE:
        return Action("block", reason="this action is disabled")
    if name == "browser_navigate" or (name == "browser_tabs" and args.get("action") == "new" and args.get("url")):
        return open_action(str(args.get("url", "")))
    if name in AUTO or name == "browser_tabs":
        return Action("read")
    if name == "browser_press_key":
        return Action("read") if args.get("key") in SAFE_KEYS else Action("type", page_site)
    if name == "browser_handle_dialog":
        return Action("type", page_site) if args.get("promptText") else Action("read")
    if name in TYPING_KINDS:
        return Action("type", page_site)
    return Action("other", page_site)


def open_action(url: str) -> Action:
    """Opening an address: blocked, a search engine's results page (a search), or an open."""
    site, why = check_url(url)
    if why:
        return Action("block", reason=why)
    if addresses.is_search(url):
        return Action("search", site or "", url)
    return Action("open", site or "", url)


@dataclass
class Form:
    title: str
    fields: list[Field]

    def apply(self, name: str, args: dict, values: list[str]) -> dict:
        """The approved values, placed back into the tool's arguments."""
        if name == "browser_type":
            return {**args, "text": values[0]}
        if name == "browser_fill_form":
            out = []
            for f, v in zip(args.get("fields") or [], values, strict=False):
                out.append({**f, "value": v})
            return {**args, "fields": out}
        if name == "browser_select_option":
            return {**args, "values": [x.strip() for x in values[0].split(",") if x.strip()]}
        if name == "browser_press_key":
            return {**args, "key": values[0]}
        if name in ("browser_navigate", "browser_tabs"):
            return {**args, "url": values[0].strip()}
        if name == "browser_handle_dialog":
            return {**args, "promptText": values[0]}
        return json.loads(values[0])


def form_for(name: str, args: dict, site: str) -> Form:
    where = f" on {site}" if site else ""
    if name == "browser_type":
        el = args.get("element")
        title = f"Type into {el}{where}?" if el else f"Type this{where}?"
        # Several lines, kept as they are, so an edit doesn't lose them (seen: a WhatsApp message's line breaks
        # lost when it was edited on a one-line field).
        return Form(title, [Field("Text", str(args.get("text", "")), "longtext")])
    if name == "browser_fill_form":
        fields = []
        for i, f in enumerate(args.get("fields") or [], 1):
            label = f.get("name") or f.get("element") or f"Field {i}"
            value = str(f.get("value", ""))
            fields.append(Field(label, value, "longtext" if "\n" in value else "text"))
        return Form(f"Fill in {len(fields)} field{'s' if len(fields) != 1 else ''}{where}?", fields)
    if name == "browser_select_option":
        el = args.get("element") or "a list"
        return Form(f"Choose in “{el}”{where}?", [Field("Option", ", ".join(args.get("values") or []))])
    if name == "browser_press_key":
        return Form(f"Press the key “{args.get('key', '')}”{where}?", [Field("Key", str(args.get("key", "")))])
    if name in ("browser_navigate", "browser_tabs"):
        return open_form(str(args.get("url", "")))
    if name == "browser_handle_dialog":
        return Form(f"Answer the page's pop-up{where}?", [Field("Text", str(args.get("promptText", "")))])
    return Form(f"Run “{name}”{where}?", [Field("Arguments", json.dumps(args, ensure_ascii=False))])


MADE_UP = "Aha! put this address together itself: it isn't in anything it read or you sent. It may be wrong."


def open_form(url: str, data: bool = False, made_up: bool = False) -> Form:
    """The card for opening an address; with what it sends when it carries data (SPEC §5.2 rule 7), and a warning
    when nothing in the task showed it (addresses.made_up)."""
    site, _ = check_url(url)
    warn = [Field("Note", MADE_UP, "info")] if made_up else []
    if data:
        return Form(
            f"Open this address? It sends what's in it to {site}",
            [Field("Address", url), Field("It sends", addresses.describe(url), "info"), *warn],
        )
    if addresses.is_search(url):
        return Form(f"Search {site} for this?", [Field("Address", url)])
    return Form(f"Open {site}?", [Field("Address", url), *warn])
