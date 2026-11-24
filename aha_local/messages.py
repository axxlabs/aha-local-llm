# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Every text aha sends on its own (not written by the model). One place, so wording changes are one edit."""

STOPPED = "Stopped."
NOTHING_TO_STOP = "Nothing is running."
FORGOT_LAST = "Done: I've dropped our last exchange from what I keep in mind."
MODEL_DOWN = "My language model isn't running right now, so I can't answer. Try again in a minute."
FAILED = "Something went wrong while working on that, sorry. I've noted the error on the computer."
RESTARTING = (
    "Aha! is restarting on your computer, so anything I was working on has stopped. Give it a minute, then ask again."
)
TOO_LONG = "That took too long, so I stopped. Try a smaller step, or tell me where to start."
TOO_MANY_STEPS = "I couldn't finish that within my step limit. Here's where I got to:"
APPROVAL_EXPIRED = "I stopped waiting for your approval, so I didn't do it."
APPROVAL_STALE = "That approval isn't open any more."
EDIT_ONE = "Send the corrected value."
EDIT_MANY = "Send the corrected fields, one per line, like:\n`{example}`"
EDIT_UNCLEAR = "I couldn't match that to the fields. Send lines like `2: new value`, or tap Approve or Decline."

WELCOME = (
    "Hi! I'm **Aha!**, a truly private personal assistant, running on your own computer. "
    "Ask me anything, or say **help**."
)
HELP = """**Aha!** is a truly private personal assistant, and it runs on your own computer. Ask me anything: \
I look things up, read web pages, book things on websites (signing in when you want: you type your password on \
a card, and I never see it), search and write your email (once it's connected), remind you of things, do \
tasks on a schedule or keep an eye on something for you, and remember what matters about you.

Before anything leaves this computer with your words or data in it (typing into a website, an email) and before \
I save something about you, I'll show it to you with buttons: **Approve**, **Edit** or **Decline**.

Commands: **stop** (cancel what I'm doing) · **memories** (what I remember about you) · **sign-ins** (sites \
my browser remembers for you) · **reminders** (reminders and scheduled tasks) · **forget last** (drop our last \
exchange) · **help**

Open source (AGPL-3.0): {source}"""

APPROVE_LABELS = {
    "action": ("Approve", "Edit", "Decline"),
    "memory": ("Save", "Edit", "Don't"),
    "forget": ("Forget it", "Edit", "Keep it"),
    "email": ("Send", "Edit", "Don't send"),
    "draft": ("Save draft", "Edit", "Don't save"),
    "signin": ("Sign in", "Edit", "Don't"),
    "check": ("Send taps", "Edit", "Give up"),
    "schedule": ("Schedule", "Edit", "Don't"),
    "calendar": ("Save", "Edit", "Don't"),
    "calendar_delete": ("Delete it", "Edit", "Keep it"),
}
EDIT_IN_CARD = "Type them into the card in the app, then tap its button."
SIGNINS = (
    "My browser remembers these sites for you, like your own browser does:\n{sites}\n\n"
    "To forget one (and be signed out there): **forget sign-in** and the site, e.g. forget sign-in example.com. "
    "To forget everything: **clear browser**, or Settings → Your browser."
)
SIGNED_IN = " (signed in)"
NO_SIGNINS = (
    "My browser doesn't remember any site for you yet. When a site asks you to sign in, I'll show you a sign-in card."
)
SIGNIN_FORGOTTEN = "Done: I've forgotten {site}, so you're signed out there."
SIGNIN_UNKNOWN = "My browser doesn't remember {site} for you. Say **sign-ins** to see the list."
BROWSER_CLEARED = "Done: my browser has forgotten every site for you, so you're signed out everywhere."
REMINDERS = "Your reminders and scheduled tasks:\n{items}\n\nTo remove one: **cancel reminder** and its id."
NO_REMINDERS = 'You have no reminders or scheduled tasks. Ask me, e.g. "remind me at 6 to call Sam".'
REMINDER_CANCELLED = "Cancelled."
REMINDER_UNKNOWN = "I couldn't find that one. Say **reminders** to see the list with ids."


# Outbound messages are signed, in code (SPEC §3.4): on the card, so the person sees it and can change it.
def signature(name: str) -> str:
    return f"Written by Aha!\n- {name}'s Private AI Assistant"


def signed(text: str, name: str) -> str:
    """A message to someone else, ending with the signature on its own lines (never twice). On a messaging site
    the new lines are typed as Shift+Enter, so they don't send the message early."""
    text = text.strip()
    return text if "written by aha" in text.lower() else f"{text}\n\n{signature(name)}".strip()
