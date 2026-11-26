# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""What a background task may do without asking (SPEC §3.4 "Background tasks", §5.2 "Allowed up front")."""

from __future__ import annotations

from aha_local.approvals import Decision, Field
from aha_local.policy.gate import Action, Facts, decide
from aha_local.policy.grants import Grant
from aha_local.tools import background, guard
from aha_local.tools.registry import TaskState, ToolContext

from .conftest import person, settle


def test_the_gate_runs_what_was_allowed_and_nothing_else():
    g = Grant(open={"linkedin.com"}, type={"thefork.co.uk"}, search=True, email={"sam@example.com"}, calendar_add=True)
    f = Facts(private=True, granted=g)
    assert decide(Action("type", site="thefork.co.uk"), f).why == "granted"
    assert decide(Action("open", site="thefork.co.uk", url="https://thefork.co.uk/?name=Fe"), f).why == "granted"
    assert decide(Action("open", site="thefork.co.uk", url="https://thefork.co.uk/r/1"), f).why == "granted"
    assert decide(Action("type", site="opentable.com"), f).outcome == "card"
    # Opening allowed is reading only: no typing there, and no address that carries data to it.
    assert decide(Action("open", site="linkedin.com", url="https://linkedin.com/in/sam"), f).why == "granted"
    assert decide(Action("type", site="linkedin.com"), f).outcome == "card"
    assert decide(Action("open", site="linkedin.com", url="https://linkedin.com/?q=Fe"), f).outcome == "card"
    closed = Facts(allow_all_sites=False, granted=g)
    assert decide(Action("open", site="linkedin.com", url="https://linkedin.com/"), closed).why == "granted"
    assert decide(Action("open", site="bbc.co.uk", url="https://bbc.co.uk/"), closed).outcome == "card"
    assert decide(Action("search"), f).why == "granted"
    email = lambda *to: Action("send", what="email", to=to)  # noqa: E731
    assert decide(email("sam@example.com"), f).why == "granted"
    assert decide(email("sam@example.com", "ana@example.com"), f).outcome == "card"  # everyone, or a card
    assert decide(email(), f).outcome == "card"
    assert decide(Action("send", what="calendar", to=()), f).why == "granted"
    assert decide(Action("send", what="calendar", to=("ana@example.com",)), f).outcome == "card"
    # Adding isn't changing, and a draft isn't a send.
    assert decide(Action("send", what="calendar_change", to=()), f).outcome == "card"
    assert decide(Action("send", what="draft", to=("ana@example.com",)), f).outcome == "card"
    assert decide(Action("send", what="draft", to=("sam@example.com",)), f).why == "granted"
    drafts = Facts(granted=Grant(drafts=True))
    assert decide(Action("send", what="draft", to=("ana@example.com",)), drafts).why == "granted"
    assert decide(Action("send", what="email", to=("ana@example.com",)), drafts).outcome == "card"
    for what in ("memory", "task", "calendar_delete"):  # nothing allows these: always their own card
        assert decide(Action("send", what=what, to=()), f).outcome == "card"
    assert decide(Action("send", site="whatsapp.com"), f).outcome == "card"  # a message typed on a site
    assert decide(Action("search"), Facts(private=True)).outcome == "card"


class Asked:
    def __init__(self) -> None:
        self.cards: list[str] = []

    async def ask(self, account, kind, title, fields, image=None, why=""):
        self.cards.append(kind)
        return Decision(True, [f.value for f in fields])


async def test_an_allowed_send_skips_its_card_and_says_what_went(accounts, config):
    said: list[str] = []

    async def send(text, image=None):
        said.append(text)

    fe = person(accounts)
    asked = Asked()
    state = TaskState(grant=Grant(email={"sam@example.com"}))
    ctx = ToolContext(fe, asked, config, state, None, send)  # type: ignore[arg-type]
    fields = [Field("To", "Sam <sam@example.com>"), Field("Subject", "Saturday"), Field("Message", "Hi", "longtext")]
    d, refused = await guard.send(ctx, "email", "Send this email?", fields, to=["sam@example.com"])
    assert d.approved and refused is None and d.values == ["Sam <sam@example.com>", "Saturday", "Hi"]
    assert asked.cards == [] and said == [
        "✉️ Send this email, as you allowed for this task. To: Sam <sam@example.com>; Subject: Saturday"
    ]
    assert any('"granted"' in str(line) for line in fe.vault.records(fe.dir / "audit.jsonl"))
    await guard.send(ctx, "email", "Send this email?", fields, to=["ana@example.com"])
    await guard.send(ctx, "memory", "Remember this?", [Field("Fact", "x")])
    assert asked.cards == ["email", "memory"]


async def test_a_task_asks_on_its_own_card_and_its_checks_keep_what_stays_ticked(agent, llm, chat, accounts, registry):
    background.register(registry)
    fe = person(accounts)
    llm.call(
        "task_create",
        title="Parcel",
        prompt="On fedex.com, type 385413566727 in the tracking box and read the status.",
        every_minutes=60,
        until="never",
        type=["https://www.fedex.com/en-gb"],
        open=["fedex.com", "royalmail.com"],
        email=["Sam@Example.com"],
    )
    llm.answer("Watching it.")
    await agent.inbound(fe, "tell me when my FedEx parcel moves, and email Sam")
    card = await chat.next()
    assert card.card["kind"] == "task"
    ticks = [(f["label"], f["value"]) for f in card.card["fields"] if f["kind"] == "bool"]
    assert ticks == [  # typing on fedex.com includes opening it: one tick
        ("Type and fill in forms on fedex.com (searches, sign-ups, bookings)", "true"),
        ("Open pages on royalmail.com (read only), even after reading your email or private pages", "true"),
        ("Send email to sam@example.com", "true"),
    ]
    values = [f["value"] for f in card.card["fields"]]
    values[-1] = "false"  # the person unticks the email
    await agent.approval_reply(fe, card.card["id"], "approve", values)
    await settle(agent)
    (entry,) = background.load(fe)
    g = background.grant_of(fe, entry["id"])
    assert g.type == {"fedex.com"} and g.open == {"royalmail.com"} and not g.email
    assert len([m for m in chat.sent if m.card]) == 1  # one card: the task's own


async def test_a_task_refuses_what_isnt_a_site_or_one_address(agent, llm, chat, accounts, registry):
    background.register(registry)
    fe = person(accounts)
    llm.call("task_create", title="X", prompt="Check x", every_minutes=60, until="never", type=["not a site"])
    llm.answer("OK.")
    await agent.inbound(fe, "watch it")
    await settle(agent)
    result = [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"][-1]
    assert result.startswith("Not created: not a site isn't a site's name") and not any(m.card for m in chat.sent)


async def test_a_request_the_person_watches_asks_for_every_send(agent, llm, chat, accounts):
    """Seen: an email sent with no review, because it had been allowed up front in a simple request. Only a
    background task's checks run with permissions; here every email shows its card."""
    fe = person(accounts)
    llm.call("memory_add", text="Likes tea")
    llm.answer("OK.")
    await agent.inbound(fe, "I like tea")
    card = await chat.next()
    assert card.card["kind"] == "memory" and "replan" not in card.card
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)
    assert "permissions_request" not in agent.registry.names()


def test_a_task_stored_before_the_split_keeps_what_it_allowed():
    g = Grant.from_dict({"sites": ["royalmail.com"], "email": ["sam@example.com"], "calendar": True})
    assert g.type == {"royalmail.com"} and g.email == {"sam@example.com"} and g.calendar_add and g.calendar_change
    assert Grant.from_dict(g.to_dict()) == g


async def test_changing_an_event_needs_its_own_permission(accounts, config):
    asked = Asked()
    ctx = ToolContext(person(accounts), asked, config, TaskState(grant=Grant(calendar_add=True)), None, None)  # type: ignore[arg-type]
    fields = [Field("Title", "Dinner")]
    await guard.send(ctx, "calendar", "Add this?", fields, to=[])
    await guard.send(ctx, "calendar", "Change “Dinner”?", fields, to=[], what="calendar_change")
    assert asked.cards == ["calendar"]
