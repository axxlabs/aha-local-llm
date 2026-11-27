---
name: coordination
description: Arranging something with other people (a time, a meeting, a handover) by email, WhatsApp, LinkedIn or a site's messages, until it's settled.
---
# Coordinating with people

When the user asks you to arrange something with someone, you see it through until it's settled.

1. Make the goal and its limits clear: what must be agreed, and what the user accepts (the days, times, place, price). Ask only if the request doesn't say.
2. Send the first message on the channel the user named, or the one they already use with that person: email, WhatsApp, LinkedIn, or a site's messages.
3. Then create a background task with task_create that listens for the reply and carries on the conversation:
   - title: who and what, e.g. "Sam: dinner on Friday".
   - prompt: who the person is and the channel (the email thread's subject, or the chat's name on which site), the goal and its limits word for word, what was sent and when, the date three days after the last message sent (DD/MM/YYYY), and the steps below.
   - every_minutes: 15.
   - done_when: the goal is agreed and confirmed to the user.
4. Each check:
   - Read the conversation. Nothing new: report waiting. Nothing new three days after the last message sent: report failed, saying who didn't answer.
   - They replied within the limits: answer them on the same channel to move it forward, and report waiting (or done once it's agreed, with what was agreed).
   - They propose anything outside the limits, or ask something you can't answer from the goal: don't answer that part. Report notify with what they said and what you need from the user. The task goes on.
   - Agreed: confirm to them, do what follows (the calendar event, the booking), and report done with the details.
5. Message only the people in the goal, only in that conversation.
