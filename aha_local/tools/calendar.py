# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Google Calendar through its REST API (SPEC §3.4 "Calendar").

The same Google connection as email (tools/google.py): one OAuth client per computer, one token per person, looked
up from the account running the task. Reading marks the task as holding private data (SPEC §5.2); adding,
changing and deleting events are `send` actions, always shown to the person with the exact event.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from .. import dates, messages
from ..approvals import Field
from . import google, guard
from .registry import Registry, Tool, ToolContext, ToolResult, params, string

API = "https://www.googleapis.com/calendar/v3"
OWN = {"", "primary", "main", "my calendar", "my own", "mine", "default", "your calendar", "personal"}
MAX_EVENTS = 25
MAX_TEXT = 4_000
NO_CALENDAR = (
    "The calendar isn't connected for this person yet: their Google connection was made before Aha! could use "
    "calendars. Tell them to run `aha mail connect <their name>` again on the computer and allow the calendar on "
    "Google's page. Don't suggest any other way."
)


class NoCalendar(Exception):
    pass


async def api(ctx: ToolContext, method: str, path: str, conn: dict | None = None, **kw) -> dict:
    r = await google.request(ctx, method, API + path, conn, **kw)
    if r.status_code == 403 and "insufficient" in r.text.lower():
        raise NoCalendar
    r.raise_for_status()
    return r.json() if r.content else {}


async def call(ctx: ToolContext, method: str, path: str, conn: dict | None = None, **kw) -> dict | ToolResult:
    """The API call, or the result that explains why it couldn't be made."""
    try:
        return await api(ctx, method, path, conn, **kw)
    except google.NotConnected:
        return ToolResult(google.NOT_CONNECTED, error=True)
    except NoCalendar:
        return ToolResult(NO_CALENDAR, error=True)
    except httpx.HTTPStatusError as e:
        return ToolResult(f"Google Calendar answered HTTP {e.response.status_code}.", error=True)


# ---- which calendar: the name the person used, looked up in their own list, never an id from the model


@dataclass(frozen=True)
class Cal:
    id: str
    name: str
    writable: bool = True
    conn: dict | None = field(default=None, compare=False)  # the connected account it's in (SPEC §3.4)
    several: bool = False  # the person has several connected accounts: say which one

    @property
    def events(self) -> str:
        return f"/calendars/{quote(self.id, safe='')}/events"

    @property
    def label(self) -> str:
        if self.id == "primary":
            return f"your calendar ({self.conn['email']})" if self.conn else "your calendar"
        return f"the “{self.name}” calendar" + (f" ({self.conn['email']})" if self.conn else "")


def own(ctx: ToolContext, conn: dict | None = None) -> Cal:
    """The primary calendar of a connected account (the first one by default)."""
    conn = conn or google.pick(ctx.account)
    several = len(google.connections(ctx.account)) > 1
    return Cal("primary", conn["email"] if conn else "your calendar", True, conn, several)


async def calendars(ctx: ToolContext) -> list[Cal] | ToolResult:
    """Every calendar of every connected account (SPEC §3.4 "Connections"); the own ones named by their address."""
    conns = google.connections(ctx.account)
    if not conns:
        return ToolResult(google.NOT_CONNECTED, error=True)
    several = len(conns) > 1
    cals: list[Cal] = []
    for conn in conns:
        found = await call(ctx, "GET", "/users/me/calendarList", conn, params={"maxResults": 250})
        if isinstance(found, ToolResult):
            if not several:
                return found
            continue
        for c in found.get("items", []):
            if not (isinstance(c, dict) and c.get("id")):
                continue
            if c.get("primary"):
                cals.append(own(ctx, conn))
                continue
            title = c.get("summaryOverride") or c.get("summary") or c["id"]
            cals.append(Cal(c["id"], title, c.get("accessRole") in ("owner", "writer"), conn, several))
    return cals


async def which(ctx: ToolContext, name) -> Cal | ToolResult:
    """The calendar the person named ("Family"), or their own when none was named."""
    name = " ".join(str(name or "").split())
    if name.lower() in OWN:
        return own(ctx)
    cals = await calendars(ctx)
    if isinstance(cals, ToolResult):
        return cals
    wanted = name.lower().removesuffix(" calendar").strip()
    for match in (
        [c for c in cals if c.name.lower() == wanted],
        [c for c in cals if wanted in c.name.lower()],
    ):
        if len(match) == 1:
            return match[0]
    names = ", ".join(f"“{c.name}”" for c in cals) or "none"
    return ToolResult(
        f"There's no single calendar called “{name}”. The user's calendars: {names}. Use one of these names, or ask "
        "the user which one.",
        error=True,
    )


def read_only(cal: Cal) -> ToolResult:
    return ToolResult(
        f"{cal.label.capitalize()} is read-only for the user: they can see it but not change it.", error=True
    )


CALENDAR_ARG = string(
    "Optional: the calendar's name as the user said it, e.g. 'Family', or one of their addresses for that account's "
    "own calendar; their own (first account) if not given"
)


# ---- times


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
    return dates.editable(when.astimezone(tz))


def day_field(start: str) -> Field:
    """Which day it is, on the card, in words: the person sees at once if it's the wrong one."""
    return Field("Day", dates.in_words(start), "info")


def describe(event: dict, tz) -> str:
    # No weekday: the model never writes one (it guesses them wrong); the app shows each date with its weekday.
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
    return found if all(google.EMAIL.fullmatch(g) for g in found) else None


# ---- reading


async def calendar_list(args: dict, ctx: ToolContext) -> ToolResult:
    tz = dates.tz(ctx)
    start = dates.when_of(args.get("start"), tz)
    if start is None:
        return ToolResult("Give start as 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (local time).", error=True)
    end = dates.when_of(args.get("end"), tz) if args.get("end") else None
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
    if args.get("calendar"):
        cal = await which(ctx, args.get("calendar"))
        cals = cal if isinstance(cal, ToolResult) else [cal]
    else:
        cals = await calendars(ctx)  # every calendar of every account (SPEC §3.4): "Family" too
    if isinstance(cals, ToolResult):
        return cals
    out, empty, failed = [], [], []
    for cal in cals:
        found = await call(ctx, "GET", cal.events, cal.conn, params=query)
        if isinstance(found, ToolResult):
            if len(cals) == 1:
                return found
            failed.append(cal.label)
            continue
        events = found.get("items", [])
        if events:  # always under its calendar: the answer says which account (SPEC §3.4)
            out.append(f"Events in {cal.label}:\n" + "\n".join(describe(e, tz) for e in events))
        else:
            empty.append(cal.label)
    span = f"from {dates.editable(start)} to {dates.editable(end)}"
    if empty:
        out.append(f"No events {span} in: " + "; ".join(empty) + ".")
    if failed:
        out.append("Couldn't read: " + "; ".join(failed) + ".")
    return ToolResult("\n\n".join(out))


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
    start, end = dates.when_of(start_s, tz), dates.when_of(end_s, tz) if end_s else None
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
    tz = dates.tz(ctx)
    start = dates.when_of(args.get("start"), tz)
    if start is None:
        return ToolResult("Give start as 'YYYY-MM-DD HH:MM' (local time), or 'YYYY-MM-DD' for a whole day.", error=True)
    end = dates.when_of(args.get("end"), tz) if args.get("end") else None
    cal = await which(ctx, args.get("calendar"))
    if isinstance(cal, ToolResult):
        return cal
    if not cal.writable:
        return read_only(cal)
    fields = event_fields(
        " ".join(str(args.get("title", "")).split()),
        dates.editable(start),
        dates.editable(end) if end else "",
        " ".join(str(args.get("location", "") or "").split()),
        messages.signed(str(args.get("notes", "") or ""), medium="event", limit=MAX_TEXT),
        ", ".join(guests_of(str(args.get("guests", "") or "")) or []),
    )
    fields += [Field("Calendar", cal.name, "info"), day_field(fields[1].value)]
    invite = " and invite the guests" if fields[5].value else ""
    decision, refused = await guard.send(ctx, "calendar", f"Add this to {cal.label}{invite}?", fields)
    if refused:
        return refused
    body = event_body(decision.values, tz)
    if isinstance(body, str):
        return ToolResult(f"Not added: {body}. Ask the user.", error=True)
    sends = "all" if body["attendees"] else "none"
    made = await call(ctx, "POST", cal.events, cal.conn, params={"sendUpdates": sends}, json=body)
    if isinstance(made, ToolResult):
        return made
    return ToolResult(
        f"Added to {cal.label}: " + describe(made, tz) + ("; invitations sent." if sends == "all" else ".")
    )


async def _event(ctx: ToolContext, args: dict) -> tuple[Cal, dict] | ToolResult:
    """The event and its calendar, read from Google: the card shows what's really there. Looked for in the calendar
    named; when it isn't there, in every calendar of theirs (seen: an event in "Family" moved with no calendar
    named got "not found", and the model added a copy instead)."""
    event_id = str(args.get("id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,1024}", event_id):
        return ToolResult("Give the event's id from calendar_list.", error=True)
    cal = await which(ctx, args.get("calendar"))
    if isinstance(cal, ToolResult):
        return cal
    event = await call(ctx, "GET", f"{cal.events}/{event_id}", cal.conn)
    if isinstance(event, ToolResult) and "HTTP 404" in event.text:
        everywhere = await calendars(ctx)
        for other in everywhere if isinstance(everywhere, list) else []:
            if other.id == cal.id and other.conn == cal.conn:
                continue
            found = await call(ctx, "GET", f"{other.events}/{event_id}", other.conn)
            if not isinstance(found, ToolResult) and found.get("status") != "cancelled":
                cal, event = other, found
                break
    if isinstance(event, ToolResult):
        if "HTTP 404" in event.text:
            return ToolResult(
                "No event has that id in any of the user's calendars. Find it with calendar_list; don't add a new one.",
                error=True,
            )
        return event
    if not cal.writable:
        return read_only(cal)
    return cal, event


async def calendar_update(args: dict, ctx: ToolContext) -> ToolResult:
    tz = dates.tz(ctx)
    found = await _event(ctx, args)
    if isinstance(found, ToolResult):
        return found
    cal, event = found

    def new(key: str, current: str) -> str:
        value = args.get(key)
        return current if value in (None, "") else " ".join(str(value).split())

    current_guests = ", ".join(a.get("email", "") for a in event.get("attendees", []) if not a.get("self"))
    start = dates.when_of(args.get("start"), tz) if args.get("start") else None
    end = dates.when_of(args.get("end"), tz) if args.get("end") else None
    if (args.get("start") and start is None) or (args.get("end") and end is None):
        return ToolResult("Give times as 'YYYY-MM-DD HH:MM' (local time), or 'YYYY-MM-DD' for a whole day.", error=True)
    fields = event_fields(
        new("title", event.get("summary", "")),
        dates.editable(start) if start else from_api(event.get("start", {}), tz),
        dates.editable(end) if end else from_api(event.get("end", {}), tz),
        new("location", event.get("location", "")),
        str(args.get("notes") or event.get("description", "") or "").strip()[:MAX_TEXT],
        new("guests", current_guests),
    )
    fields += [Field("Calendar", cal.name, "info"), day_field(fields[1].value)]
    decision, refused = await guard.send(ctx, "calendar", f"Change “{event.get('summary', 'this event')}”?", fields)
    if refused:
        return refused
    body = event_body(decision.values, tz)
    if isinstance(body, str):
        return ToolResult(f"Not changed: {body}. Ask the user.", error=True)
    sends = "all" if body["attendees"] or current_guests else "none"
    done = await call(ctx, "PATCH", f"{cal.events}/{event['id']}", cal.conn, params={"sendUpdates": sends}, json=body)
    if isinstance(done, ToolResult):
        return done
    return ToolResult("Changed: " + describe(done, tz) + ("; guests were told." if sends == "all" else "."))


async def calendar_delete(args: dict, ctx: ToolContext) -> ToolResult:
    tz = dates.tz(ctx)
    found = await _event(ctx, args)
    if isinstance(found, ToolResult):
        return found
    cal, event = found
    guests = [a.get("email", "") for a in event.get("attendees", []) if not a.get("self")]
    fields = [Field("Event", describe(event, tz).split("] ", 1)[-1], "info"), Field("Calendar", cal.name, "info")]
    if guests:
        fields.append(Field("Guests told", ", ".join(guests), "info"))
    _, refused = await guard.send(ctx, "calendar_delete", f"Delete this event from {cal.label}?", fields)
    if refused:
        return refused
    done = await call(
        ctx, "DELETE", f"{cal.events}/{event['id']}", cal.conn, params={"sendUpdates": "all" if guests else "none"}
    )
    if isinstance(done, ToolResult):
        return done
    return ToolResult(f"Deleted “{event.get('summary', '')}” from {cal.label}.")


WHEN = "Local time 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' for a whole day"


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "calendar_list",
            "The user's Google Calendars (all of them, or one of theirs by name): events from start to end (one "
            "day if no end), optionally matching "
            "words. Returns ids, titles, times, places and guests. Use it for 'am I free…?', 'what's on…?', and "
            "before booking anything at a time.",
            params(
                ["start"],
                start=string(WHEN),
                end=string("Optional, the same format; a date means through the end of that day"),
                query=string("Optional words to match, e.g. 'dentist'"),
                calendar=CALENDAR_ARG,
            ),
            calendar_list,
            private=True,
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
                calendar=CALENDAR_ARG,
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
                calendar=string("The calendar the event is on, if not the user's own (as in calendar_list)"),
            ),
            calendar_update,
            private=True,
        )
    )
    reg.add(
        Tool(
            "calendar_delete",
            "Delete an event from the user's calendar by its id from calendar_list.",
            params(
                ["id"],
                id=string("The event's id"),
                calendar=string("The calendar the event is on, if not the user's own (as in calendar_list)"),
            ),
            calendar_delete,
            private=True,
        )
    )
