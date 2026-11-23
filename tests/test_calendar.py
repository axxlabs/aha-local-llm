# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Google Calendar (SPEC §3.4 "Calendar"), against a fake Calendar API with two people's calendars."""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
import pytest

from aha_local.approvals import Decision
from aha_local.policy.gate import Action, Facts, decide
from aha_local.tools import calendar, mail

from .test_mail import FakeApprovals, ctx_for, two  # noqa: F401  (the fixture)

EVENTS = {
    "at-fe": {
        "ev1": {
            "id": "ev1",
            "summary": "Dentist",
            "start": {"dateTime": "2026-11-26T09:30:00Z"},
            "end": {"dateTime": "2026-11-26T10:00:00Z"},
            "location": "High St",
        }
    },
    "at-hb": {"ev9": {"id": "ev9", "summary": "Haibo's private thing", "start": {"date": "2026-11-26"}}},
}
FAMILY = "fam123@group.calendar.google.com"
FAMILY_EVENTS = {"ev5": {"id": "ev5", "summary": "School play", "start": {"date": "2026-11-27"}}}
CALENDARS = [
    {"id": "fe@example.com", "summary": "fe@example.com", "accessRole": "owner", "primary": True},
    {"id": FAMILY, "summary": "Family", "accessRole": "writer"},
    {"id": "en.uk#holiday@group.v.calendar.google.com", "summary": "Holidays in the UK", "accessRole": "reader"},
]


class FakeCalendar:
    def __init__(self, scoped: bool = True) -> None:
        self.scoped = scoped
        self.writes: list[tuple[str, str, dict, dict]] = []  # method, path, params, body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(mail.TOKEN_URL):
            from urllib.parse import parse_qs

            token = {"rt-fe": "at-fe", "rt-hb": "at-hb"}[parse_qs(request.content.decode())["refresh_token"][0]]
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
        box = EVENTS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if box is None:
            return httpx.Response(401)
        if not self.scoped:  # a token from before the calendar was asked for
            return httpx.Response(403, json={"error": {"message": "Request had insufficient authentication scopes."}})
        params = dict(request.url.params)
        if request.url.path == "/calendar/v3/users/me/calendarList":
            return httpx.Response(200, json={"items": CALENDARS})
        raw = request.url.raw_path.decode().split("?")[0].removeprefix("/calendar/v3/calendars/")
        cal_id, _, path = raw.partition("/")
        path = "/" + path
        if cal_id == quote(FAMILY, safe=""):
            box = FAMILY_EVENTS
        elif cal_id != "primary":
            return httpx.Response(404)
        self.calendar = cal_id
        if path == "/events" and request.method == "GET":
            q = params.get("q", "").lower()
            return httpx.Response(200, json={"items": [e for e in box.values() if q in json.dumps(e).lower()]})
        if path.startswith("/events/") and request.method == "GET":
            event = box.get(path.split("/")[2])
            return httpx.Response(200, json=event) if event else httpx.Response(404)
        body = json.loads(request.content) if request.content else {}
        self.writes.append((request.method, path, params, body))
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "new1", **body})


@pytest.fixture
def cal(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(mail, "TRANSPORT", httpx.MockTransport(fake))
    mail._tokens.clear()
    return fake


async def test_reading_reaches_only_the_running_persons_calendar_and_marks_the_task(cal, two, config):  # noqa: F811
    fe, hb = two
    ctx = ctx_for(fe, config)
    found = await calendar.calendar_list({"start": "2026-11-26"}, ctx)
    assert "[ev1] Dentist" in found.text and "at High St" in found.text and "Haibo" not in found.text
    assert ctx.task.read_calendar and ctx.task.private  # new sites and searches now ask first (SPEC §5.2)
    other = await calendar.calendar_list({"start": "2026-11-26"}, ctx_for(hb, config))
    assert "Haibo's private thing" in other.text and "Dentist" not in other.text
    assert (await calendar.calendar_list({"start": "next tuesday"}, ctx)).error


async def test_adding_is_always_a_card_and_what_was_approved_is_what_is_saved(cal, two, config):  # noqa: F811
    fe, _ = two
    edited = ["Dinner at Flat Iron", "2026-11-27 20:00", "", "Kings Cross", "Ref FI-77", ""]
    approvals = FakeApprovals(Decision(True, values=edited))
    result = await calendar.calendar_add(
        {"title": "Dinner", "start": "2026-11-27 19:30", "location": "Kings Cross", "notes": "Ref FI-77"},
        ctx_for(fe, config, approvals),
    )
    card = approvals.asked[0]
    assert card.kind == "calendar" and card.title == "Add this to your calendar?"
    assert card.fields[:3] == [
        ("What", "Dinner", "text"),
        ("Starts", "2026-11-27 19:30", "text"),
        ("Ends", "", "text"),
    ]
    method, path, params, body = cal.writes[0]
    assert (method, path, params["sendUpdates"]) == ("POST", "/events", "none")
    assert body["summary"] == "Dinner at Flat Iron" and body["start"]["dateTime"].startswith("2026-11-27T20:00")
    assert body["end"]["dateTime"].startswith("2026-11-27T21:00")  # an hour, when no end was given
    assert "Added to your calendar" in result.text and "approv" not in result.text and "card" not in result.text


async def test_guests_get_invitations_only_through_the_card(cal, two, config):  # noqa: F811
    fe, _ = two
    approvals = FakeApprovals()  # declines
    result = await calendar.calendar_add(
        {"title": "Lunch", "start": "2026-11-28 13:00", "guests": "sam@example.com"}, ctx_for(fe, config, approvals)
    )
    assert result.error and "the user stopped it" in result.text and not cal.writes
    assert approvals.asked[0].title == "Add this to your calendar and invite the guests?"
    good = ["Lunch", "2026-11-28 13:00", "", "", "", "sam@example.com"]
    await calendar.calendar_add({"title": "Lunch", "start": "x"}, ctx_for(fe, config, FakeApprovals()))
    await calendar.calendar_add(
        {"title": "Lunch", "start": "2026-11-28 13:00"}, ctx_for(fe, config, FakeApprovals(Decision(True, values=good)))
    )
    _, _, params, body = cal.writes[-1]
    assert params["sendUpdates"] == "all" and body["attendees"] == [{"email": "sam@example.com"}]
    bad = ["Lunch", "2026-11-28 13:00", "", "", "", "not an address"]
    refused = await calendar.calendar_add(
        {"title": "Lunch", "start": "2026-11-28 13:00"}, ctx_for(fe, config, FakeApprovals(Decision(True, values=bad)))
    )
    assert refused.error and len(cal.writes) == 1


async def test_changing_and_deleting_are_cards_on_the_real_event(cal, two, config):  # noqa: F811
    fe, _ = two
    change = FakeApprovals(
        Decision(True, values=["Dentist", "2026-11-26 11:00", "2026-11-26 11:30", "High St", "", ""])
    )
    await calendar.calendar_update({"id": "ev1", "start": "2026-11-26 11:00"}, ctx_for(fe, config, change))
    assert change.asked[0].title == "Change “Dentist”?" and change.asked[0].fields[0] == ("What", "Dentist", "text")
    assert cal.writes[0][:2] == ("PATCH", "/events/ev1")
    delete = FakeApprovals(Decision(True, values=["x"]))
    done = await calendar.calendar_delete({"id": "ev1"}, ctx_for(fe, config, delete))
    assert delete.asked[0].kind == "calendar_delete" and cal.writes[-1][:2] == ("DELETE", "/events/ev1")
    assert "Deleted “Dentist”" in done.text
    assert (await calendar.calendar_delete({"id": "ev9"}, ctx_for(fe, config))).error  # Haibo's: not Fe's
    assert (await calendar.calendar_delete({"id": "../x"}, ctx_for(fe, config))).error


async def test_a_connection_from_before_the_calendar_says_to_connect_again(monkeypatch, two, config):  # noqa: F811
    monkeypatch.setattr(mail, "TRANSPORT", httpx.MockTransport(FakeCalendar(scoped=False)))
    mail._tokens.clear()
    fe, _ = two
    result = await calendar.calendar_list({"start": "2026-11-26"}, ctx_for(fe, config))
    assert result.error and "aha mail connect" in result.text and "again" in result.text


def test_calendar_changes_are_sends():
    """SPEC §5.2 rule 2: always a card, trusted sites or not."""
    assert decide(Action("send"), Facts(trusted={"google.com"})).outcome == "card"
    assert "https://www.googleapis.com/auth/calendar.events" in mail.SCOPES


async def test_the_calendar_is_chosen_by_the_name_the_person_used(cal, two, config):  # noqa: F811
    fe, _ = two
    ctx = ctx_for(fe, config)
    family = await calendar.calendar_list({"start": "2026-11-27", "calendar": "family calendar"}, ctx)
    assert "School play" in family.text and "Family" in family.text and "Dentist" not in family.text
    approvals = FakeApprovals(Decision(True, values=["Bake sale", "2026-11-29", "", "", "", "", "Family"]))
    added = await calendar.calendar_add(
        {"title": "Bake sale", "start": "2026-11-29", "calendar": "Family"}, ctx_for(fe, config, approvals)
    )
    assert approvals.asked[0].title == "Add this to the “Family” calendar?"
    assert ("Calendar", "Family", "info") in approvals.asked[0].fields
    assert cal.calendar == quote(FAMILY, safe="") and "Added to the “Family” calendar" in added.text
    unknown = await calendar.calendar_list({"start": "2026-11-27", "calendar": "Work"}, ctx)
    assert unknown.error and "“Family”" in unknown.text and "“Holidays in the UK”" in unknown.text
    holidays = FakeApprovals()
    ro = await calendar.calendar_add(
        {"title": "x", "start": "2026-11-29", "calendar": "holidays"}, ctx_for(fe, config, holidays)
    )
    assert ro.error and "read-only" in ro.text and not holidays.asked  # never a card for what can't be done
    moved = await calendar.calendar_delete(
        {"id": "ev5", "calendar": "Family"}, ctx_for(fe, config, FakeApprovals(Decision(True, values=["", ""])))
    )
    assert "Deleted “School play” from the “Family” calendar" in moved.text
