# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Every text aha sends on its own (not written by the model). One place, so wording changes are one edit."""

STOPPED = "Stopped."
NOTHING_TO_STOP = "Nothing is running."
FORGOT_LAST = "Done: I've dropped our last exchange from what I keep in mind."
MODEL_DOWN = "My language model isn't running right now, so I can't answer. Try again in a minute."
FAILED = "Something went wrong while working on that, sorry. I've noted the error on the computer."
TOO_LONG = "That took too long, so I stopped. Try a smaller step, or tell me where to start."
TOO_MANY_STEPS = "I couldn't finish that within my step limit. Here's where I got to:"
APPROVAL_EXPIRED = "I stopped waiting for your approval, so I didn't do it."
APPROVAL_STALE = "That approval isn't open any more."
EDIT_ONE = "Send the corrected value."
EDIT_MANY = "Send the corrected fields, one per line, like:\n`{example}`"
EDIT_UNCLEAR = "I couldn't match that to the fields. Send lines like `2: new value`, or tap Approve or Decline."

WELCOME = "Hi! I'm **Aha!**, your private agent, running on your own computer. Ask me anything, or say **help**."
HELP = """**Aha!** is your private agent, and it runs on your own computer. Ask me anything: I look things up, \
read web pages, book things on websites (signing in when you want: you type your password on a card, and I \
never see it), search and write your email (once it's connected) and remember what matters about you.

Before anything leaves this computer with your words or data in it (typing into a website, an email) and before \
I save something about you, I'll show it to you with buttons: **Approve**, **Edit** or **Decline**.

Commands: **stop** (cancel what I'm doing) · **memories** (what I remember about you) · **sign-ins** (sites \
you stay signed in on) · **forget last** (drop our last exchange) · **help**

Open source (AGPL-3.0): {source}"""

APPROVE_LABELS = {
    "action": ("Approve", "Edit", "Decline"),
    "memory": ("Save", "Edit", "Don't"),
    "forget": ("Forget it", "Edit", "Keep it"),
    "email": ("Send", "Edit", "Don't send"),
    "draft": ("Save draft", "Edit", "Don't save"),
    "signin": ("Sign in", "Edit", "Don't"),
}
EDIT_IN_CARD = "Type them into the card in the app, then tap its button."
SIGNINS = (
    "You're kept signed in on:\n{sites}\n\n"
    "To sign out of one: **forget sign-in** and the site, e.g. forget sign-in example.com"
)
NO_SIGNINS = "You aren't kept signed in on any site. When a site asks you to sign in, I'll show you a sign-in card."
SIGNIN_FORGOTTEN = "Signed out of {site}: I no longer keep you signed in there."
SIGNIN_UNKNOWN = "You weren't kept signed in on {site}. Say **sign-ins** to see the list."
