# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Chats are user-driven (SPEC §5.2 rule 2, 2026-11-27: "typing on whatsapp should always be user-driven… or any
other chat too").

A message typed into a chat, on any site, is a card every time: no "Always allow for this task", no trust. On a
chat site (WhatsApp Web), so is everything typed there, its search box included. The card names the chat the
message goes to, read from the page by the harness, never from the model: on a chat site by the site's own reader
below (fixed code, run through `browser_run_code_unsafe`); elsewhere from the page's address or the message box's
label. On a chat site, a message whose chat can't be read isn't typed at all.
"""

from __future__ import annotations

import re

# The open chat's name, from the conversation pane's header (`#main header`): its first `span[dir=auto]`, the title
# WhatsApp Web shows above the messages. Empty when no chat is open. Fixed code: nothing from the model goes in.
WHATSAPP = (
    "async (page) => { const h = page.locator('#main header').first(); "
    "if (!(await h.count())) return JSON.stringify({ chat: '' }); "
    "const s = h.locator('span[dir=\"auto\"]').first(); "
    "const t = (await s.count()) ? await s.innerText({ timeout: 2000 }) "
    ": (await h.innerText({ timeout: 2000 })).split('\\n')[0]; "
    "return JSON.stringify({ chat: t || '' }); }"
)

# Sites that are chats as a whole (their registrable domain), with the code that reads which chat is open.
CHAT_SITES = {"whatsapp.com": WHATSAPP}
MAX_NAME = 80

# The message box's own label names its chat too (seen in real WhatsApp Web snapshots, 2026-11-27: "Type a message
# to Haibo E", "Type a message to Meta AI"): a second reading from the page, to check the first.
BOX_NAME = {"whatsapp.com": re.compile(r"^\s*type a message to (.+?)\s*$", re.I)}


def is_chat_site(site: str) -> bool:
    return site in CHAT_SITES


def user_driven(kind: str, site: str) -> bool:
    """Every time a card, never an "always" or trust: a message typed on a site, or any typing on a chat site."""
    return bool(site) and (kind == "send" or (kind == "type" and is_chat_site(site)))


def clean(name: object) -> str:
    """A chat's name as the page gave it, on one line, printable, short."""
    if not isinstance(name, str):
        return ""
    return " ".join("".join(ch for ch in name if ch.isprintable()).split())[:MAX_NAME]


def named_by_box(site: str, labels: list[str]) -> str:
    """The chat the message box's own label names, or ""."""
    pattern = BOX_NAME.get(site)
    for label in labels if pattern else []:
        if m := pattern.match(label or ""):
            return clean(m.group(1))
    return ""


def resolve(site: str, header: str, labels: list[str]) -> str:
    """The chat a message goes to: the header's name (the site's reader), checked against the message box's label
    when it names one; either alone if only one reads. "" when neither does, or they disagree: then nothing is typed."""
    header, box = clean(header), named_by_box(site, labels)
    if header and box and header.casefold() != box.casefold():
        return ""
    return header or box
