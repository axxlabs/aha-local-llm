# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Browser tool calls as actions for the gate (SPEC §5.2), and the cards that show them (§5.3).

Decided from the real arguments of each call, never from the model's descriptions. Clicks and reading can't
carry new information out of the computer; typing and addresses can.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from .. import messages
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


# Each browser tool that can carry words out, in one place (SPEC §5.2, §5.3): when a call of it types, the card
# that shows it, how the approved values go back into its arguments, which page elements it targets, and what was
# entered, said back to the model as plain fact. Adding or changing such a tool is one entry here.


def _fill_fields(args: dict) -> list[dict]:
    return [f for f in args.get("fields") or [] if isinstance(f, dict)]


def _fill_form(args: dict, where: str) -> Form:
    fields = []
    for i, f in enumerate(_fill_fields(args), 1):
        label = f.get("name") or f.get("element") or f"Field {i}"
        value = str(f.get("value", ""))
        fields.append(Field(label, value, "longtext" if "\n" in value else "text"))
    return Form(f"Fill in {len(fields)} field{'s' if len(fields) != 1 else ''}{where}?", fields)


def _type_form(args: dict, where: str) -> Form:
    el = args.get("element")
    title = f"Type into {el}{where}?" if el else f"Type this{where}?"
    # Several lines, kept as they are, so an edit doesn't lose them (seen: a WhatsApp message's line breaks
    # lost when it was edited on a one-line field).
    return Form(title, [Field("Text", str(args.get("text", "")), "longtext")])


def _filled(args: dict, labels: list[str]) -> str:
    parts = [
        f"{label or f.get('name') or 'a field'}: “{f.get('value', '')}”"
        for f, label in zip(_fill_fields(args), labels, strict=False)
    ]
    return ("Filled in: " + "; ".join(parts) + ".") if parts else ""


@dataclass(frozen=True)
class Typing:
    types: Callable[[dict], bool]  # whether this call carries words out (else it's a read)
    form: Callable[[dict, str], Form]  # the card: (args, " on <site>")
    apply: Callable[[dict, list[str]], dict]  # the approved values back into the arguments
    entered: Callable[[dict, list[str]], str]  # what went, said as fact: (args, the targets' page labels)
    targets: Callable[[dict], list[str]] = lambda a: [str(a.get("target", ""))]  # elements typed into: checked
    checks_targets: bool = True  # the targets must be in the latest snapshot and not a sign-in field


TYPING: dict[str, Typing] = {
    "browser_type": Typing(
        types=lambda a: True,
        form=_type_form,
        apply=lambda a, v: {**a, "text": v[0]},
        entered=lambda a, _: f"Typed: “{a.get('text', '')}”.",
    ),
    "browser_fill_form": Typing(
        types=lambda a: True,
        form=_fill_form,
        apply=lambda a, v: {**a, "fields": [{**f, "value": x} for f, x in zip(a.get("fields") or [], v, strict=False)]},
        entered=_filled,
        targets=lambda a: [str(f.get("target", "")) for f in _fill_fields(a)],
    ),
    "browser_select_option": Typing(
        types=lambda a: True,
        form=lambda a, where: Form(
            f"Choose in “{a.get('element') or 'a list'}”{where}?", [Field("Option", ", ".join(a.get("values") or []))]
        ),
        apply=lambda a, v: {**a, "values": [x.strip() for x in v[0].split(",") if x.strip()]},
        entered=lambda a, _: "Chose: " + ", ".join(str(v) for v in a.get("values") or []) + ".",
    ),
    "browser_press_key": Typing(
        types=lambda a: a.get("key") not in SAFE_KEYS,
        form=lambda a, where: Form(
            f"Press the key “{a.get('key', '')}”{where}?", [Field("Key", str(a.get("key", "")))]
        ),
        apply=lambda a, v: {**a, "key": v[0]},
        entered=lambda a, _: f"Pressed: {a.get('key', '')}.",
        checks_targets=False,
    ),
    "browser_handle_dialog": Typing(
        types=lambda a: bool(a.get("promptText")),
        form=lambda a, where: Form(f"Answer the page's pop-up{where}?", [Field("Text", str(a.get("promptText", "")))]),
        apply=lambda a, v: {**a, "promptText": v[0]},
        entered=lambda a, _: f"Answered the pop-up: “{a.get('promptText', '')}”.",
        checks_targets=False,
    ),
}


def opens(name: str, args: dict) -> bool:
    """The call opens an address: the address goes through the gate's open rules (guard.open)."""
    return name == "browser_navigate" or (
        name == "browser_tabs" and args.get("action") == "new" and bool(args.get("url"))
    )


def classify(name: str, args: dict, page_site: str = "", labels: list[str] | None = None) -> Action:
    """A browser tool call as an action for the gate (SPEC §5.2). `page_site` is the current page's site, read
    from the browser's own address, and `labels` the page's own labels of the elements it types into, never
    the model's words. Typing on a messaging site is a send (rule 2)."""
    if name in BLOCK or name in HIDE:
        return Action("block", reason="this action is disabled")
    if opens(name, args):
        return open_action(str(args.get("url", "")))
    if name in TYPING:
        if not TYPING[name].types(args):
            return Action("read")
        if labels is not None and sends_message(name, page_site, labels):
            return Action("send", page_site)
        return Action("type", page_site)
    if name in AUTO or name == "browser_tabs":
        return Action("read")
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
        if name in TYPING:
            return TYPING[name].apply(args, values)
        if opens(name, args):
            return {**args, "url": values[0].strip()}
        return json.loads(values[0])


def form_for(name: str, args: dict, site: str, signer: str = "") -> Form:
    """The card for a browser call. `signer`: it sends a message on a messaging site, signed in that name in
    code (SPEC §3.4), on the card, so the person sees the signature and can change it."""
    where = f" on {site}" if site else ""
    if name in TYPING:
        form = TYPING[name].form(args, where)
        if signer:
            form.title = f"Send this message on {site}?"
            for f in form.fields:
                if f.label == "Text" or MESSAGE_BOX.search(f.label):
                    f.value = messages.signed(f.value, signer, medium="chat")
        return form
    if opens(name, args):
        return open_form(str(args.get("url", "")))
    return Form(f"Run “{name}”{where}?", [Field("Arguments", json.dumps(args, ensure_ascii=False))])


def open_form(url: str, data: bool = False) -> Form:
    """The card for opening an address; with what it sends when it carries data (SPEC §5.2 rule 7). An address the
    model made up never gets here: it's refused (tools/guard.open_address)."""
    site, _ = check_url(url)
    if data:
        return Form(
            f"Open this address? It sends what's in it to {site}",
            [Field("Address", url), Field("It sends", addresses.describe(url), "info")],
        )
    if addresses.is_search(url):
        return Form(f"Search {site} for this?", [Field("Address", url)])
    return Form(f"Open {site}?", [Field("Address", url)])
