---
name: background-tasks
description: Reminders, things at a time, and waiting for something to happen (a reply, a parcel, a price).
---
# Checking later, on its own

topic_defer is the only way to do anything later: it sends the topic you're in to the background, and it wakes up in this same topic. Use it without asking first for reminders, things at a time or on repeat, and anything waiting on something else (a reply, tracking, a page changing, tickets going on sale, a price).

- prompt: how to check and what to do, complete on its own: the site by name and how to find the page there, the names and codes, and what to do once it happens.
- every_minutes or at: as often as it can change and matters.
- done_when: what ends it, or never.
- Each wakeup sees this topic's conversation: what the user writes here later changes what it does.
- Wakeups run only when nobody is using sha: when they want an exact time, say it may run a little late.
- To coordinate with other people, read the coordination skill.
