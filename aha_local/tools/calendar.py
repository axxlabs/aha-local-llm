# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Google Calendar through its REST API (SPEC §3.4 "Calendar").

The same Google connection as email (tools/mail.py): one OAuth client per computer, one token per person, looked
up from the account running the task. Reading marks the task as holding private data (SPEC §5.2); adding,
changing and deleting events are `send` actions, always shown to the person with the exact event.
"""

from __future__ import annotations

import datetime as dt
import re

import httpx

from ..approvals import Field
from . import guard, mail
from .registry import Registry, Tool, ToolContext, ToolResult, params, string
from .schedule import parse_local, tz_of

CALENDAR = "https://www.googleapis.com/calendar/v3/calendars/primary"
MAX_EVENTS = 25
MAX_TEXT = 4_000
EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
NO_CALENDAR = (
    "The calendar isn't connected for this person yet: their Google connection was made before Aha! could use "
    "calendars. Tell them to run `aha mail connect <their name>` again on the computer and allow the calendar on "
    "Google's page. Don't suggest any other way."
)


class NoCalendar(Exception):
    pass


async def api(ctx: ToolContext, method: str, path: str, **kw) -> dict:
    token = await mail.access_token(ctx)
    async with mail._http() as http:
        r = await http.request(method, CALENDAR + path, headers={"Authorization": f"Bearer {token}"}, **kw)
    if r.status_code == 401:
        mail.forget_token(ctx.account.id)
    if r.status_code == 403 and "insufficient" in r.text.lower():
        raise NoCalendar
    r.raise_for_status()
    return r.json() if r.content else {}


async def call(ctx: ToolContext, method: str, path: str, **kw) -> dict | ToolResult:
    """The API call, or the result that explains why it couldn't be made."""
    try:
        return await api(ctx, method, path, **kw)
    except mail.NotConnected:
        return ToolResult(mail.NOT_CONNECTED, error=True)
    except NoCalendar:
        return ToolResult(NO_CALENDAR, error=True)
    except httpx.HTTPStatusError as e:
        return ToolResult(f"Google Calendar answered HTTP {e.response.status_code}.", error=True)


# ---- times


def when_of(value, tz) -> dt.datetime | dt.date | None:
    """'2026-11-24 19:00' → a local time; '2026-11-24' → a whole day."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            return None
    return parse_local(text, tz)


def to_api(when: dt.datetime | dt.date, tz) -> dict:
    if isinstance(when, dt.datetime):
        return {"dateTime": when.isoformat(), "timeZone": str(tz.key)}
    return {"date": when.isoformat()}


def from_api(value: dict, tz) -> str:
    if "date" in value:
        return value["date"]
    if not value.get("dateTime"):
        return "?"
    when = dt.datetime.fromisoformat(value.get("dateTime", "").replace("Z", "+00:00"))
    return when.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def shown(value: dt.datetime | dt.date) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, dt.datetime) else value.isoformat()


def describe(event: dict, tz) -> str:
    start, end = from_api(event.get("start", {}), tz), from_api(event.get("end", {}), tz)
    parts = [f"[{event.get('id', '')}] {event.get('summary', '(no title)')}: {start} to {end}"]
    if event.get("location"):
        parts.append(f"at {event['location']}")
    guests = [a.get("email", "") for a in event.get("attendees", []) if not a.get("self")]
    if guests:
        parts.append(f"with {', '.join(guests)}")
    return ", ".join(parts)


def guests_of(text: str) -> list[str] | None:
    found = [g.strip() for g in re.split(r"[,;\s]+", text or "") if g.strip()]
    return found if all(EMAIL.match(g) for g in found) else None


# ---- reading


async def calendar_list(args: dict, ctx: ToolContext) -> ToolResult:
    tz = tz_of(ctx)
    start = when_of(args.get("start"), tz)
    if start is None:
        return ToolResult("Give start as 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (local time).", error=True)
    end = when_of(args.get("end"), tz) if args.get("end") else None
    if isinstance(start, dt.date) and not isinstance(start, dt.datetime):
        start = dt.datetime.combine(start, dt.time(), tz)
    if end is None:
        end = start + dt.timedelta(days=1)
    elif not isinstance(end, dt.datetime):
        end = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), tz)  # through the end of that day
    query = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true"}
    query |= {"orderBy": "startTime", "maxResults": MAX_EVENTS}
    if text := " ".join(str(args.get("query", "") or "").split()):
        query["q"] = text
    found = await call(ctx, "GET", "/events", params=query)
    if isinstance(found, ToolResult):
        return found
    ctx.task.read_calendar = True  # the task now holds private data (SPEC §5.2)
    events = found.get("items", [])
    if not events:
        return ToolResult(f"No events from {shown(start)} to {shown(end)}.")
    return ToolResult("\n".join(describe(e, tz) for e in events))


# ---- changing (a send: always a card, SPEC §5.2)


def event_fields(title, start, end, where, notes, guests) -> list[Field]:
    return [
        Field("What", title),
        Field("Starts", start),
        Field("Ends", end),
        Field("Where", where),
        Field("Notes", notes, "longtext"),
        Field("Guests", guests),
    ]


def event_body(values: list[str], tz) -> dict | str:
    """The event from the card's values, or what's wrong with them."""
    title, start_s, end_s, where, notes, guests_s = (v.strip() for v in values[:6])
    start, end = when_of(start_s, tz), when_of(end_s, tz) if end_s else None
    if not title or start is None:
        return "the event needs a title and a start ('YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' for a whole day)"
    if end is None:
        end = start + (dt.timedelta(hours=1) if isinstance(start, dt.datetime) else dt.timedelta(days=1))
    if type(end) is not type(start) or end <= start:
        return "the end must be after the start, and both times or both whole days"
    guests = guests_of(guests_s)
    if guests is None:
        return "the guests must be email addresses"
    body = {"summary": title[:300], "start": to_api(start, tz), "end": to_api(end, tz)}
    body |= {"location": where[:300], "description": notes[:MAX_TEXT]}
    body["attendees"] = [{"email": g} for g in guests]
    return body


async def calendar_add(args: dict, ctx: ToolContext) -> ToolResult:
    tz = tz_of(ctx)
    start = when_of(args.get("start"), tz)
    if start is None:
        return ToolResult("Give start as 'YYYY-MM-DD HH:MM' (local time), or 'YYYY-MM-DD' for a whole day.", error=True)
    end = when_of(args.get("end"), tz) if args.get("end") else None
    fields = event_fields(
        " ".join(str(args.get("title", "")).split()),
        shown(start),
        shown(end) if end else "",
        " ".join(str(args.get("location", "") or "").split()),
        str(args.get("notes", "") or "").strip()[:MAX_TEXT],
        ", ".join(guests_of(str(args.get("guests", "") or "")) or []),
    )
    title = "Add this to your calendar and invite the guests?" if fields[5].value else "Add this to your calendar?"
    decision, refused = await guard.ask(ctx, "calendar", title, fields)
    if refused:
        return refused
    body = event_body(decision.values, tz)
    if isinstance(body, str):
        return ToolResult(f"Not added: {body}. Ask the user.", error=True)
    sends = "all" if body["attendees"] else "none"
    made = await call(ctx, "POST", "/events", params={"sendUpdates": sends}, json=body)
    if isinstance(made, ToolResult):
        return made
    return ToolResult(
        "Added to the calendar: " + describe(made, tz) + ("; invitations sent." if sends == "all" else ".")
    )


async def _event(ctx: ToolContext, event_id: str) -> dict | ToolResult:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,1024}", event_id):
        return ToolResult("Give the event's id from calendar_list.", error=True)
    return await call(ctx, "GET", f"/events/{event_id}")


async def calendar_update(args: dict, ctx: ToolContext) -> ToolResult:
    tz = tz_of(ctx)
    event = await _event(ctx, str(args.get("id", "")))
    if isinstance(event, ToolResult):
        return event
    ctx.task.read_calendar = True

    def new(key: str, current: str) -> str:
        value = args.get(key)
        return current if value in (None, "") else " ".join(str(value).split())

    current_guests = ", ".join(a.get("email", "") for a in event.get("attendees", []) if not a.get("self"))
    start = when_of(args.get("start"), tz) if args.get("start") else None
    end = when_of(args.get("end"), tz) if args.get("end") else None
    if (args.get("start") and start is None) or (args.get("end") and end is None):
        return ToolResult("Give times as 'YYYY-MM-DD HH:MM' (local time), or 'YYYY-MM-DD' for a whole day.", error=True)
    fields = event_fields(
        new("title", event.get("summary", "")),
        shown(start) if start else from_api(event.get("start", {}), tz),
        shown(end) if end else from_api(event.get("end", {}), tz),
        new("location", event.get("location", "")),
        str(args.get("notes") or event.get("description", "") or "").strip()[:MAX_TEXT],
        new("guests", current_guests),
    )
    decision, refused = await guard.ask(ctx, "calendar", f"Change “{event.get('summary', 'this event')}”?", fields)
    if refused:
        return refused
    body = event_body(decision.values, tz)
    if isinstance(body, str):
        return ToolResult(f"Not changed: {body}. Ask the user.", error=True)
    sends = "all" if body["attendees"] or current_guests else "none"
    done = await call(ctx, "PATCH", f"/events/{event['id']}", params={"sendUpdates": sends}, json=body)
    if isinstance(done, ToolResult):
        return done
    return ToolResult("Changed: " + describe(done, tz) + ("; guests were told." if sends == "all" else "."))


async def calendar_delete(args: dict, ctx: ToolContext) -> ToolResult:
    tz = tz_of(ctx)
    event = await _event(ctx, str(args.get("id", "")))
    if isinstance(event, ToolResult):
        return event
    ctx.task.read_calendar = True
    guests = [a.get("email", "") for a in event.get("attendees", []) if not a.get("self")]
    fields = [Field("Event", describe(event, tz).split("] ", 1)[-1], "info")]
    if guests:
        fields.append(Field("Guests told", ", ".join(guests), "info"))
    _, refused = await guard.ask(ctx, "calendar_delete", "Delete this event from your calendar?", fields)
    if refused:
        return refused
    done = await call(ctx, "DELETE", f"/events/{event['id']}", params={"sendUpdates": "all" if guests else "none"})
    if isinstance(done, ToolResult):
        return done
    return ToolResult(f"Deleted “{event.get('summary', '')}” from the calendar.")


WHEN = "Local time 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' for a whole day"


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "calendar_list",
            "The user's own Google Calendar: events from start to end (one day if no end), optionally matching "
            "words. Returns ids, titles, times, places and guests. Use it for 'am I free…?', 'what's on…?', and "
            "before booking anything at a time.",
            params(
                ["start"],
                start=string(WHEN),
                end=string("Optional, the same format; a date means through the end of that day"),
                query=string("Optional words to match, e.g. 'dentist'"),
            ),
            calendar_list,
        )
    )
    reg.add(
        Tool(
            "calendar_add",
            "Add an event to the user's own calendar. Guests get an invitation from the user's address.",
            params(
                ["title", "start"],
                title=string("What it is, e.g. 'Dinner at Flat Iron'"),
                start=string(WHEN),
                end=string("Optional; an hour after the start (or one day) if not given"),
                location=string("Optional place or address"),
                notes=string("Optional details, e.g. the booking reference"),
                guests=string("Optional guests' email addresses, comma-separated"),
            ),
            calendar_add,
        )
    )
    reg.add(
        Tool(
            "calendar_update",
            "Change an event in the user's calendar by its id from calendar_list; give only what changes.",
            params(
                ["id"],
                id=string("The event's id"),
                title=string("Optional new title"),
                start=string("Optional new start, " + WHEN),
                end=string("Optional new end"),
                location=string("Optional new place"),
                notes=string("Optional new details"),
                guests=string("Optional: the full new guest list, comma-separated"),
            ),
            calendar_update,
        )
    )
    reg.add(
        Tool(
            "calendar_delete",
            "Delete an event from the user's calendar by its id from calendar_list.",
            params(["id"], id=string("The event's id")),
            calendar_delete,
        )
    )
