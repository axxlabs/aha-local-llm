---
name: calendar
description: Reading and changing the user's calendar.
---
# Calendar

- Check calendar_list when they ask what's on or whether they're free, and before booking something at a time.
- Times are local, as in the <context> block. Say which calendar each event is in.
- Change an event with calendar_update and its id from calendar_list: never add a second copy. If the change fails, say so and ask.
- Add guests only when asked: they get an invitation from the user's address.
