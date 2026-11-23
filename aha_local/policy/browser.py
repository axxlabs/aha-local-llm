# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Browser policy (SPEC §5.2): which browser tool calls run, need approval, or are blocked.

Decided from the real arguments of each call, never from the model's descriptions. Clicks and reading can't
carry new information out of the computer; typing and URLs can.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from ..approvals import Field
from .urls import check_url

Outcome = Literal["auto", "approval", "block"]

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


# Typing on the page: auto on a site the person trusts (SPEC §5.7), never in a task that has read mail (§5.5).
TRUSTABLE = {"browser_type", "browser_fill_form", "browser_select_option", "browser_press_key", "browser_handle_dialog"}


def runs_trusted(name: str, page_site: str, trusted: set[str] | list[str], read_mail: bool) -> bool:
    """Whether an action that needs approval runs without a card because the person trusts the page's site.

    `page_site` comes from the browser's own address, never from the model."""
    return name in TRUSTABLE and not read_mail and bool(page_site) and page_site in trusted


def offered(name: str) -> bool:
    return name not in BLOCK and name not in HIDE


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    reason: str = ""
    site: str | None = None


def decide(name: str, args: dict, allow_all_sites: bool, approved_sites: set[str]) -> Verdict:
    if name in BLOCK or name in HIDE:
        return Verdict("block", "this action is disabled")
    if name == "browser_navigate" or (name == "browser_tabs" and args.get("action") == "new" and args.get("url")):
        site, why = check_url(str(args.get("url", "")))
        if why:
            return Verdict("block", why)
        if allow_all_sites or site in approved_sites:
            return Verdict("auto", site=site)
        return Verdict("approval", site=site)
    if name == "browser_tabs":
        return Verdict("auto")
    if name == "browser_press_key":
        return Verdict("auto") if args.get("key") in SAFE_KEYS else Verdict("approval")
    if name == "browser_handle_dialog":
        return Verdict("approval") if args.get("promptText") else Verdict("auto")
    if name in AUTO:
        return Verdict("auto")
    return Verdict("approval")  # browser_type, browser_fill_form, browser_select_option, and anything new


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
        return Form(title, [Field("Text", str(args.get("text", "")))])
    if name == "browser_fill_form":
        fields = []
        for i, f in enumerate(args.get("fields") or [], 1):
            label = f.get("name") or f.get("element") or f"Field {i}"
            fields.append(Field(label, str(f.get("value", ""))))
        return Form(f"Fill in {len(fields)} field{'s' if len(fields) != 1 else ''}{where}?", fields)
    if name == "browser_select_option":
        el = args.get("element") or "a list"
        return Form(f"Choose in “{el}”{where}?", [Field("Option", ", ".join(args.get("values") or []))])
    if name == "browser_press_key":
        return Form(f"Press the key “{args.get('key', '')}”{where}?", [Field("Key", str(args.get("key", "")))])
    if name in ("browser_navigate", "browser_tabs"):
        site_new, _ = check_url(str(args.get("url", "")))
        return Form(f"Open {site_new}, a site not opened yet?", [Field("Address", str(args.get("url", "")))])
    if name == "browser_handle_dialog":
        return Form(f"Answer the page's pop-up{where}?", [Field("Text", str(args.get("promptText", "")))])
    return Form(f"Run “{name}”{where}?", [Field("Arguments", json.dumps(args, ensure_ascii=False))])
