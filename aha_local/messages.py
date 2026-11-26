# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Every text aha sends on its own (not written by the model). One place, so wording changes are one edit."""

import re

STOPPED = "Stopped."
NOTHING_TO_STOP = "Nothing is running."
MODEL_DOWN = "My language model isn't running right now, so I can't answer. Try again in a minute."
FAILED = "Something went wrong while working on that, sorry. I've noted the error on the computer."
RESTARTING = (
    "Aha! is restarting on your computer, so anything I was working on has stopped. Give it a minute, then ask again."
)
TOO_MANY_STEPS = "I couldn't finish that within my step limit. What I did:"
APPROVAL_STALE = "That approval isn't open any more."
EDIT_UNCLEAR = "I couldn't use those values. Check the card and tap Approve or Decline again."

WELCOME = (
    "Hi! I'm **Aha!**, a truly private personal assistant, running on your own computer. "
    "Ask me anything, or say **help**."
)
HELP = """**Aha!** is a truly private personal assistant, and it runs on your own computer. Ask me anything: \
I look things up, read web pages, book things on websites (signing in when you want: you type your password on \
a card, and I never see it), search and write your email (once it's connected), keep checking on things \
for you in the background (a reply, a delivery, a price, or something to do at a time), and remember what matters \
about you.

Before anything leaves this computer with your words or data in it (typing into a website, an email) and before \
I save something about you, I'll show it to you with buttons: **Approve**, **Edit** or **Decline**.

Say **stop** to cancel what I'm doing. What I remember about you, the sites my browser keeps and your background \
tasks are in the app's Settings and Tasks.

Open source (AGPL-3.0): {source}"""

# What Aha! writes for someone else is signed, in code (SPEC §3.4): on the card, so the person sees it and can
# change it. One signature per medium, and one way to tell it's already there.
SIGNATURES = {
    "email": "Written by Aha!\n- {name}'s Private AI Assistant",
    # A message typed on a site: one line, plain (WhatsApp's italics showed as underscores elsewhere)
    "chat": "(Written by Aha!, {name}'s Private AI Assistant)",
    "event": "Created by Aha! (A Private AI Assistant)",  # read by guests too: no one's name
}
SEPARATORS = {"email": "\n\n", "chat": "\n", "event": "\n\n"}
# Not \b at the start: an older chat signature opens with "(_Written", and "_" is a word character.
ALREADY_SIGNED = re.compile(r"(?<![a-z])(?:written|created) by aha\b", re.I)


def signature(name: str, medium: str = "email") -> str:
    return SIGNATURES[medium].format(name=name)


def signed(text: str, name: str = "", medium: str = "email", limit: int | None = None) -> str:
    """Text for someone else, ending with the signature (never twice), within `limit` characters with the
    signature kept whole. On a messaging site a new line is typed as Shift+Enter, so it doesn't send early."""
    text = text.strip()
    if ALREADY_SIGNED.search(text):
        return text[:limit] if limit else text
    sig, sep = signature(name, medium), SEPARATORS[medium]
    if limit:
        text = text[: max(0, limit - len(sig) - len(sep))]
    return f"{text}{sep}{sig}".strip()


# A tool the model starts using, as a grey line in the chat (SPEC §7.4 "Tools used"). None: not shown (the time,
# a message it sends mid-task, which shows itself).
TOOL_LABELS = {
    "web_search": "web search",
    "fetch_url": "web page",
    "conversation_search": "conversation history",
    "show_screenshot": "screenshot",
    "browser_login": "sign-in",
    "browser_human_check": "human check",
    "current_time": None,
    "message_user": None,
}
TOOL_GROUPS = (
    ("browser_", "browser"),
    ("email_", "email"),
    ("calendar_", "calendar"),
    ("memory_", "memory"),
    ("task_", "background tasks"),
)


def tool_label(name: str) -> str | None:
    if name in TOOL_LABELS:
        return TOOL_LABELS[name]
    return next((label for prefix, label in TOOL_GROUPS if name.startswith(prefix)), name.replace("_", " "))
