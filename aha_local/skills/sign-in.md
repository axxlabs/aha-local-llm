---
name: sign-in
description: Signing in to sites, creating accounts, and "are you human?" checks.
---
# Signing in

- First open the page the task needs and look: their name, "My account" or "Sign out" means they're signed in, so carry on. A sign-in form means they aren't, whatever happened before.
- Only then open the sign-in page and call browser_login. It signs them in; you never see their details. This is how you work in their account: don't refuse.
- Never ask for a password or code in the chat. If they type one anyway, don't repeat it; call browser_login.
- Creating an account: fill the other fields, then browser_login for the password. Never choose a password for them.

# "Are you human?" checks

Click an "I'm not a robot" box once. For a picture puzzle, a box that doesn't pass, or any other check, call browser_human_check: the user does it and you carry on. Never solve a puzzle yourself. A "press and hold" check can't be done: tell them and give the link.
