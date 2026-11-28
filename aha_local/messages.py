# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Every text aha sends on its own (not written by the model). One place, so wording changes are one edit."""

import re

STOPPED = "Stopped."
NOTHING_TO_STOP = "Nothing is running."
MODEL_DOWN = "My language model isn't running right now, so I can't answer. Try again in a minute."
FAILED = "Something went wrong while working on that, sorry. I've noted the error on the computer."
RESTARTING = (
    "sha is restarting on your computer, so anything I was working on has stopped. Give it a minute, then ask again."
)
TOO_MANY_STEPS = "I couldn't finish that within my step limit."
APPROVAL_STALE = "That approval isn't open any more."
EDIT_UNCLEAR = "I couldn't use those values. Check the card and tap Approve or Decline again."

WELCOME = (
    "Hi! I'm **sha**, a truly private personal assistant, running on your own computer. "
    "Ask me anything, or say **help**."
)
HELP = """**sha** is a truly private personal assistant, and it runs on your own computer. Ask me anything: \
I look things up, read web pages, book things on websites (signing in when you want: you type your password on \
a card, and I never see it), search and write your email (once it's connected), keep checking on things \
for you in the background (a reply, a delivery, a price, or something to do at a time), and remember what matters \
about you.

Before anything leaves this computer with your words or data in it (typing into a website, an email) and before \
I save something about you, I'll show it to you with buttons: **Approve**, **Edit** or **Decline**.

Say **stop** to cancel what I'm doing. What I remember about you, the sites my browser keeps and your background \
tasks are in the app's Settings and Tasks.

Open source (AGPL-3.0): {source}"""

# What sha writes for someone else is signed, in code (SPEC §3.4): on the card, so the person sees it and can
# change it. One signature per medium, and one way to tell it's already there.
SIGNATURES = {
    "email": "Written by sha\n- {name}'s Private AI Assistant",
    # A message typed on a site: one line, plain (WhatsApp's italics showed as underscores elsewhere)
    "chat": "(Written by sha, {name}'s Private AI Assistant)",
    "event": "Created by sha (A Private AI Assistant)",  # read by guests too: no one's name
}
SEPARATORS = {"email": "\n\n", "chat": "\n", "event": "\n\n"}
# Not \b at the start: an older chat signature opens with "(_Written", and "_" is a word character.
ALREADY_SIGNED = re.compile(r"(?<![a-z])(?:written|created) by sha\b", re.I)


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
    "skill_read": "skill",
    "topic_read": "earlier topic",
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


# What every tool call did, as the person reads it in the chat (SPEC §7.4 "What sha did"): one line per call,
# from the call itself, never the model's words. A call that changed something says it in its own words (its
# `did`); the others say what they looked at. Email and calendar reads never show what they found.
def _host(url: str) -> str:
    rest = str(url).split("://", 1)[-1]
    return rest[:80] + ("…" if len(rest) > 80 else "")


def skill_line(name: str) -> str:
    """A skill read, by the model or added by sha for a site or tool (SPEC §3.5 "Skills"): its file's name."""
    return f"Read skill {name}.md"


CALL_WORDS = {
    "web_search": lambda a: f"Searched Google for “{a.get('query', '')}”",
    "fetch_url": lambda a: f"Read {_host(a.get('url', ''))}",
    "browser_navigate": lambda a: f"Opened {_host(a.get('url', ''))}",
    "browser_navigate_back": lambda a: "Went back a page",
    "browser_snapshot": lambda a: "Read the page",
    "browser_read_more": lambda a: "Read more of the page",
    "browser_take_screenshot": lambda a: "Looked at the page",
    "browser_find": lambda a: f"Looked for “{a.get('text', '')}” on the page",
    "browser_wait_for": lambda a: "Waited for the page",
    "browser_tabs": lambda a: "Looked at the browser's tabs",
    "browser_hover": lambda a: "Pointed at something on the page",
    "browser_mouse_wheel": lambda a: "Scrolled the page",
    "browser_mouse_click_xy": lambda a: "Clicked on the page",
    "browser_press_key": lambda a: f"Pressed {a.get('key', 'a key')}",
    "show_screenshot": lambda a: "Sent you a screenshot",
    "email_search": lambda a: "Searched your email",
    "email_read": lambda a: "Read an email",
    "calendar_list": lambda a: "Looked at your calendar",
    "conversation_search": lambda a: f"Searched our earlier conversation for “{a.get('query', '')}”",
    "current_time": lambda a: "Checked the time",
    "skill_read": lambda a: skill_line(str(a.get("name", "")).strip().lower()),
    "topic_read": lambda a: f"Read topic {str(a.get('number', '')).strip().lstrip('#')}",
    "message_user": lambda a: "Sent you a message",
    "task_list": lambda a: "Looked at your background tasks",
    "check_result": lambda a: f"Reported: {a.get('outcome', '')}",
    "browser_login": lambda a: "Signed in",
    "browser_human_check": lambda a: "Asked you to do a human check",
}


def call_line(name: str, args: dict, failed: bool) -> str:
    """The line for a call that didn't change anything (or didn't work)."""
    words = CALL_WORDS.get(name)
    try:
        line = words(args) if words else f"Used {tool_label(name) or name.replace('_', ' ')}"
    except Exception:
        line = f"Used {tool_label(name) or name.replace('_', ' ')}"
    return line + (" (didn't work)" if failed else "")
