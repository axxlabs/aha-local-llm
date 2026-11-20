"""Every text aha sends on its own (not written by the model). One place, so wording changes are one edit."""

ON_IT = "On it…"
BUSY = "Busy with another request, you're next."
NOTED = "Got it, I'll take that into account."
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

HELP = """*aha* runs on your own computer. Ask me anything: I look things up, read web pages, book things on websites, \
find things in your documents and remember what matters about you.

Before I type anything into a website or save something about you, I'll ask you with buttons: *Approve*, \
*Edit* or *Decline*.

Commands: *stop* (cancel what I'm doing) · *memories* (what I remember about you) · *forget last* (drop our \
last exchange) · *help*"""

APPROVE_LABELS = {
    "action": ("✅ Approve", "✏️ Edit", "❌ Decline"),
    "memory": ("✅ Save", "✏️ Edit", "❌ Don't"),
    "forget": ("✅ Forget it", "✏️ Edit", "❌ Keep it"),
}
