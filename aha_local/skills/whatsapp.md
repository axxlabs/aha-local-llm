---
name: whatsapp
description: Reading and sending the user's WhatsApp messages with WhatsApp Web.
sites: web.whatsapp.com
---
# WhatsApp Web

Use WhatsApp Web like a person at their own computer, for what the user asked only.

- Open https://web.whatsapp.com and wait for the chats to load (it can be slow), then take a snapshot.
- Not linked (a QR code, "Log in", "Use WhatsApp on your computer"): show_screenshot with the caption "Scan this with WhatsApp on your phone: Settings → Linked devices → Link a device", wait 30 seconds with browser_wait_for, snapshot again. Still not linked: say so and stop. Never ask for a code or phone number.
- "WhatsApp is open in another window" / "Use here": ask the user first; it closes their other WhatsApp Web.
- Find a chat in the list, or type the name in the search box above it and click the result. Several match: ask which.
- Read: open the chat and snapshot. Scroll up only as far as the task needs, a few screens at most, and say what you didn't check. For "what's new", open the chats with unread messages.
- Never open, download or play photos, videos, voice notes or files: say there is one and its caption.
- Send: check the name at the top of the chat is the person meant, type in the message box, press Enter. Only what was asked, to whom: one message per request, no forwarding, never in bulk, a group only when named.
- Quote with the chat and time: "Sam, 18:40: …".
- Open only the chats the task needs, at a person's pace.
