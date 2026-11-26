# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Background tasks (SPEC §3.4 "Background tasks", test 23)."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from zoneinfo import ZoneInfo

import pytest

from aha_local import actions
from aha_local.tools import background
from aha_local.tools.background import Runner

from .conftest import person, settle

IDLE = background.IDLE_AFTER
LONDON = ZoneInfo("Europe/London")


@pytest.fixture
def runner(agent, registry):
    background.register(registry)
    return Runner(agent.accounts, agent, "Europe/London")


def task(**over) -> dict:
    now = time.time()
    return {
        "id": "abc123",
        "title": "Reply from Sam about Saturday",
        "prompt": "Search the email for a reply from sam@example.com about Saturday; done when one arrives.",
        "every": 15,
        "until": None,
        "next": now - 1,
        "created": int(now) - 3600,
        "checks": 0,
        **over,
    }


def idle(agent, seconds: float = IDLE + 1) -> None:
    agent.last_input = time.time() - seconds


async def test_creating_one_needs_a_card_with_every_detail(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    llm.call(
        "task_create",
        title="Reply from Sam about Saturday",
        prompt="Search the email for a reply from sam@example.com about Saturday.",
        every_minutes=15,
        until="never",
    )
    llm.answer("I'll watch for Sam's reply.")
    await agent.inbound(fe, "tell me when Sam answers")
    card = await chat.next()
    first = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 15 * 60))  # no start: every from now
    assert card.card["kind"] == "task" and card.card["icon"] == "⏳"
    assert card.card["buttons"] == ["Start", "Edit", "Don't"]
    assert [(f["label"], f["value"]) for f in card.card["fields"]] == [
        ("Waiting for", "Reply from Sam about Saturday"),
        ("How to check", "Search the email for a reply from sam@example.com about Saturday."),
        ("First check", first),
        ("Check every (minutes)", "15"),
        ("Until", "never"),
    ]
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    (saved,) = background.load(fe)
    assert saved["every"] == 15 and saved["until"] is None and saved["next"] > time.time() + 14 * 60


async def test_a_declined_or_too_frequent_task_starts_nothing(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    llm.call("task_create", title="X", prompt="Check x", every_minutes=1, until="never")
    llm.answer("OK.")
    await agent.inbound(fe, "watch x every minute")
    await settle(agent)
    assert "at most every 5 minutes" in str(llm.requests[-1]["messages"][-1])
    llm.call("task_create", title="X", prompt="Check x", every_minutes=5, until="never")
    llm.answer("OK.")
    await agent.inbound(fe, "every 5 then")
    card = await chat.next()
    while card.card is None:
        card = await chat.next()
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)
    assert background.load(fe) == []


async def test_checks_wait_for_idle_and_dont_reset_it(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task()])
    idle(agent, IDLE - 10)  # someone wrote 4 minutes 50 ago
    await runner.tick()
    assert not agent.busy() and not llm.requests

    idle(agent)
    since = agent.last_input
    llm.call("email_search", query="from:sam@example.com")  # not registered: an error, which is fine here
    llm.call("check_result", outcome="waiting", message="no reply from Sam yet")
    await runner.tick()
    await settle(agent)
    assert "[Background task you set up earlier, check 1" in llm.requests[0]["messages"][-1]["content"]
    assert agent.last_input == since  # a check is no input
    assert not chat.sent and not chat.tools and True not in chat.typed  # quiet while it waits
    (left,) = background.load(fe)
    assert left["last"] == "no reply from Sam yet" and left["checks"] == 1
    assert left["next"] == pytest.approx(time.time() + 15 * 60, abs=5)
    assert all(t["content"] != left["prompt"] for t in fe.load_context()["turns"])  # the turn isn't kept

    # Not due yet: nothing runs, however idle.
    await runner.tick()
    assert len(llm.requests) == 2


async def test_a_closure_goes_to_the_chat_and_ends_the_task(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task(last="no reply yet", checks=3)])
    idle(agent)
    llm.call("check_result", outcome="done", message="Sam replied: yes, see you there.")
    await runner.tick()
    await settle(agent)
    assert "reported waiting:\nno reply yet" in llm.requests[-1]["messages"][-1]["content"]
    assert "check 4" in llm.requests[-1]["messages"][-1]["content"]
    assert chat.texts() == ["Sam replied: yes, see you there."]
    assert chat.sent[0].task == {
        "kind": "done",
        "title": "Reply from Sam about Saturday",
        "prompt": task()["prompt"],  # the box's (i): the full description
        "every": 15 * 60,
        "id": "abc123",  # the Tasks panel links the task's last check to this message
    }
    # Its own topic: what was going on folded first, and the part is titled with the task's title.
    assert chat.cuts == [0] and chat.titles[0][1] == "Reply from Sam about Saturday"
    assert background.load(fe) == []

    background.save(fe, [task()])
    llm.call("check_result", outcome="failed", message="the email account isn't connected any more.")
    await runner.tick()
    await settle(agent)
    assert chat.texts()[-1].startswith("the email account") and chat.sent[-1].task["kind"] == "failed"
    assert background.load(fe) == []


async def test_notify_tells_the_person_and_the_task_goes_on(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task(title="Emails from Sam", last="last email: 'Lunch?'")])
    idle(agent)
    llm.call("check_result", outcome="notify", message="Sam wrote again: 'Running late, start without me.'")
    await runner.tick()
    await settle(agent)
    assert "check_result" in llm.requests[-1]["messages"][-1]["content"]
    assert chat.texts() == ["Sam wrote again: 'Running late, start without me.'"]
    assert chat.sent[0].task["kind"] == "update" and chat.sent[0].task["title"] == "Emails from Sam"
    (left,) = background.load(fe)
    assert left["updates"] == 1 and left["checks"] == 1
    assert left["last"] == "Sam wrote again: 'Running late, start without me.'"  # so it isn't reported twice
    assert left["next"] == pytest.approx(time.time() + 15 * 60, abs=5)
    assert background.listing(fe)[0]["updates"] == 1


async def test_a_check_that_doesnt_finish_keeps_waiting(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task()])
    idle(agent)
    for _ in range(agent.config.max_model_calls):
        llm.call("task_list")
    await runner.tick()
    await settle(agent)
    assert not chat.sent and len(background.load(fe)) == 1


async def test_an_expired_task_ends_with_a_message_and_no_model_call(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task(until=time.time() - 60, last="still 'In transit'")])
    agent.last_input = time.time()  # not idle: expiry doesn't wait for idle
    await runner.tick()
    assert not llm.requests and background.load(fe) == []
    assert chat.texts()[0].startswith("I stopped waiting on ") and chat.sent[0].task["kind"] == "expired"
    assert chat.cuts == [0]
    assert chat.texts()[0].endswith("The last check found: still 'In transit'")


async def test_a_message_stops_a_running_check_and_gets_the_model(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    background.save(fe, [task()])
    idle(agent)
    gate = asyncio.Event()
    real = llm.chat

    async def slow(messages, *a, **kw):
        if "Background task" in str(messages[-1].get("content")):
            await gate.wait()
        return await real(messages, *a, **kw)

    llm.chat = slow
    await runner.tick()
    await asyncio.sleep(0.05)
    assert agent.busy() and not agent.running  # a check runs; no person's task does
    llm.answer("Hello!")
    await agent.inbound(fe, "hi")
    await settle(agent)
    assert chat.texts() == ["Hello!"]
    (left,) = background.load(fe)
    assert left["next"] <= time.time() and left["checks"] == 0  # it runs again at the next idle time
    assert not agent.idle()


async def test_a_card_waiting_for_someone_leaves_the_model_free(runner, agent, llm, chat, accounts):
    """A request waiting days on an unanswered card mustn't hold every check back."""
    fe, hb = person(accounts), accounts.create("Hb")
    llm.call("task_create", title="X", prompt="Check x", every_minutes=5, until="never")
    await agent.inbound(fe, "watch x")
    card = await chat.next()
    assert card.card is not None and agent.running and not agent.busy() and not agent.working()
    background.save(hb, [task()])
    idle(agent)
    llm.call("check_result", outcome="waiting", message="nothing yet")
    await runner.tick()
    await asyncio.sleep(0.05)
    assert background.load(hb)[0]["checks"] == 1
    background.save(fe, [task(id="fe0001")])
    await runner.tick()  # Fe's own task waits on the card: no check of theirs starts beside it
    assert background.load(fe)[0]["checks"] == 0 and agent.session(fe).background == ""
    await agent.approval_reply(fe, card.card["id"], "decline")
    await settle(agent)


async def test_one_check_at_a_time_the_most_overdue_first(runner, agent, llm, accounts):
    fe, hb = person(accounts), accounts.create("Hb")
    background.save(fe, [task(next=time.time() - 60)])
    background.save(hb, [task(id="def456", title="Parcel", next=time.time() - 600)])
    idle(agent)
    llm.call("check_result", outcome="waiting", message="nothing yet")
    await runner.tick()
    await runner.tick()  # the first still runs, or just ended: never two at once
    await settle(agent)
    assert "Waiting for: Parcel" in llm.requests[0]["messages"][-1]["content"]


async def test_the_panel_lists_tasks_and_cancel_is_the_persons(runner, agent, accounts):
    fe = person(accounts)
    background.save(fe, [task(), task(id="def456", title="Parcel", created=1)])
    rows = background.listing(fe)
    assert [r["id"] for r in rows] == ["abc123", "def456"] and rows[0]["every"] == 15 * 60
    assert set(rows[0]) == {
        "id",
        "title",
        "prompt",
        "every",
        "until",
        "next",
        "last",
        "checks",
        "updates",
        "created",
        "now",
        "last_how",
        "allowed",
        "card",
        "grant",
    }
    assert actions.cancel_background(fe, "def456", via="settings")
    assert not actions.cancel_background(fe, "def456", via="settings")
    assert [e["id"] for e in background.load(fe)] == ["abc123"]


async def test_every_change_tells_the_open_panels(runner, agent, llm, chat, accounts, monkeypatch):
    """Seen: a task created while the Tasks panel was open showed only after a refresh."""
    fe = person(accounts)
    told: list[str] = []
    monkeypatch.setattr(background, "WATCHERS", [lambda account: told.append(account.id)])
    llm.call("task_create", title="X", prompt="Check x", every_minutes=5, until="never")
    llm.answer("OK.")
    await agent.inbound(fe, "watch x")
    card = await chat.next()
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    assert told == [fe.id]
    actions.cancel_background(fe, background.load(fe)[0]["id"], via="settings")
    assert told == [fe.id, fe.id]


async def test_an_answer_without_check_result_is_asked_again_and_never_closes_the_task(
    runner, agent, llm, chat, accounts
):
    """Seen: the task's own prompt said 'If he hasn't replied yet, say "No reply from Leonard yet"'; the check
    answered just that, and it counted as done. A check reports with check_result, a tool call, not words."""
    fe = person(accounts)
    background.save(fe, [task(title="Leonard's WhatsApp reply")])
    idle(agent)
    llm.answer("No reply from Leonard yet.")
    llm.call("check_result", outcome="waiting", message="no reply from Leonard yet")
    await runner.tick()
    await settle(agent)
    assert "You haven't called check_result" in llm.requests[-1]["messages"][-1]["content"]
    assert not chat.sent
    (left,) = background.load(fe)
    assert left["last"] == "no reply from Leonard yet" and left["checks"] == 1

    # Asked again and still without one: sent as news, and the task goes on.
    left["next"] = time.time() - 1
    background.save(fe, [left])
    llm.answer("No reply from Leonard yet.")
    llm.answer("Still nothing from Leonard.")
    await runner.tick()
    await settle(agent)
    assert chat.texts() == ["Still nothing from Leonard."] and chat.sent[0].task["kind"] == "update"
    assert len(background.load(fe)) == 1


async def test_run_now_skips_the_idle_wait_but_not_a_busy_model(runner, agent, llm, chat, accounts):
    fe = person(accounts)
    planned = time.time() + 600
    background.save(fe, [task(next=planned)])  # not due for 10 minutes
    agent.last_input = time.time()  # someone just used Aha!: not idle
    assert actions.run_background_now(fe, "abc123", via="settings")
    assert background.listing(fe)[0]["now"] is True

    agent.session(fe).task = asyncio.get_running_loop().create_future()  # a request is being worked on
    await runner.tick()
    assert not llm.requests  # waits for the model to be free
    agent.session(fe).task = None

    llm.call("check_result", outcome="waiting", message="no reply yet")
    await runner.tick()
    await settle(agent)
    assert "check 1" in llm.requests[0]["messages"][-1]["content"]
    (left,) = background.load(fe)
    assert "now" not in left and "manual" not in left and left["checks"] == 1
    assert left["next"] == planned  # an extra check: the planned one keeps its time
    assert not actions.run_background_now(fe, "nope", via="settings")


async def test_the_next_check_gets_the_last_result_whatever_it_was(runner, agent, llm, chat, accounts):
    """Each check is told, whole, what the one before it ended with: WAITING, NOTIFY, or a check that didn't
    finish or was stopped."""
    fe = person(accounts)
    background.save(fe, [task()])
    idle(agent)
    long = "the inbox shows 3 new emails, none from Sam. " * 20
    llm.call("check_result", outcome="waiting", message=long)
    await runner.tick()
    await settle(agent)
    assert background.load(fe)[0]["last"] == long.strip()  # whole, not cut to a line

    def again() -> None:
        e = background.load(fe)[0]
        e["next"] = time.time() - 1
        background.save(fe, [e])

    again()
    llm.call("check_result", outcome="notify", message="Sam wrote: 'Lunch is off.'")
    await runner.tick()
    await settle(agent)
    assert "reported waiting:\nthe inbox shows 3 new emails" in llm.requests[-1]["messages"][-1]["content"]

    again()
    start = len(llm.requests)
    for _ in range(agent.config.max_model_calls):
        llm.call("task_list")
    await runner.tick()
    await settle(agent)
    first = llm.requests[start]["messages"][-1]["content"]
    assert "reported news, and this was sent to the user:\nSam wrote: 'Lunch is off.'" in first
    assert background.load(fe)[0]["last_how"] == "unfinished"

    # Stopped by someone's message: the next check is told so.
    again()
    gate = asyncio.Event()
    real = llm.chat

    async def slow(messages, *a, **kw):
        if "Background task" in str(messages[-1].get("content")):
            await gate.wait()
        return await real(messages, *a, **kw)

    llm.chat = slow
    await runner.tick()
    await asyncio.sleep(0.05)
    llm.answer("Hi!")
    await agent.inbound(fe, "hello")
    await settle(agent)
    assert background.load(fe)[0]["last_how"] == "interrupted"
    llm.chat = real
    idle(agent)
    llm.call("check_result", outcome="waiting", message="nothing")
    await runner.tick()
    await settle(agent)
    assert "was stopped before it finished" in llm.requests[-1]["messages"][-1]["content"]


async def test_the_person_edits_what_a_task_checks(runner, agent, llm, accounts):
    """Every field: what it waits for, what it checks, how often, the next check and until when (SPEC §3.4)."""
    fe = person(accounts)
    background.save(fe, [task()])
    edit = lambda **c: actions.edit_background(fe, "abc123", c, via="settings")  # noqa: E731
    assert edit(prompt="Check the WhatsApp chat with Sam\nfor a reply.") == ""
    assert background.load(fe)[0]["prompt"] == "Check the WhatsApp chat with Sam\nfor a reply."
    later, end = time.time() + 3600, time.time() + 86400
    assert edit(title="Sam's reply", every=30 * 60, next=later, until=end) == ""
    saved = background.load(fe)[0]
    assert (saved["title"], saved["every"], saved["next"], saved["until"]) == ("Sam's reply", 30, later, end)
    assert edit(until=None) == "" and background.load(fe)[0]["until"] is None  # no end
    assert edit(prompt="   ") != ""  # never empty
    assert "at most every 5 minutes" in edit(every=60)
    assert edit(until=time.time() - 10) != ""
    assert edit(next=end + 10, until=end) == "the next check is after until"
    assert edit(next=time.time() - 3600) == "" and background.load(fe)[0]["next"] <= time.time()  # due now
    assert actions.edit_background(fe, "nope", {"title": "x"}, via="settings") == "no such task"
    edit(next=time.time() - 1)
    idle(agent)
    llm.call("check_result", outcome="waiting", message="nothing yet")
    await runner.tick()
    await settle(agent)
    assert "Check the WhatsApp chat with Sam\nfor a reply." in llm.requests[-1]["messages"][-1]["content"]


async def test_something_at_a_time_starts_then(runner, agent, llm, chat, accounts):
    """With only background tasks, 'remind me at 6' or 'every morning at 8' is a task whose first check is then."""
    fe = person(accounts)
    tomorrow = (dt.datetime.now(LONDON) + dt.timedelta(days=1)).strftime("%Y-%m-%d 18:00")
    llm.call(
        "task_create",
        title="Call Sam",
        prompt="Remind them to call Sam.",
        every_minutes=1440,
        until="never",
        start=tomorrow,
    )
    llm.answer("OK.")
    await agent.inbound(fe, "remind me to call Sam at 6 every day")
    card = await chat.next()
    assert ("First check", tomorrow) in [(f["label"], f["value"]) for f in card.card["fields"]]
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    (saved,) = background.load(fe)
    assert saved["next"] == dt.datetime.fromisoformat(tomorrow).replace(tzinfo=LONDON).timestamp()

    llm.call("task_create", title="X", prompt="Do x", every_minutes=5, until="never", start="2001-01-01 08:00")
    llm.answer("OK.")
    await agent.inbound(fe, "do x in the past")
    await settle(agent)
    assert "has already passed" in str(llm.requests[-1]["messages"][-1])


async def test_a_check_saying_it_will_check_again_isnt_a_schedule_claim(runner, agent, llm, chat, accounts):
    """A check's "I'll check again every 15 minutes" mustn't send it back to call task_create."""
    fe = person(accounts)
    background.save(fe, [task()])
    idle(agent)
    llm.call("check_result", outcome="waiting", message="no reply yet; I'll check again every 15 minutes.")
    await runner.tick()
    await settle(agent)
    sent_back = [str(r["messages"][-1].get("content")) for r in llm.requests[1:]]
    assert not any("task_create" in m for m in sent_back), sent_back


async def test_a_check_folds_first_and_a_card_in_it_leaves_no_working(runner, agent, llm, chat, accounts):
    """Seen: after a check's note, "Aha! is working…" stayed: a card in the check was answered, which said
    "working" again, and a check never says it stopped. And the check folds what was going on before it starts,
    so its card and its note are one topic of their own."""
    fe = person(accounts)
    background.save(fe, [task()])
    idle(agent)
    llm.call("task_create", title="Y", prompt="Check y", every_minutes=60, until="never")  # any card will do
    llm.call("check_result", outcome="done", message="Sam replied.")
    await runner.tick()
    card = await chat.next()
    assert card.card is not None and chat.cuts == [0]  # folded before the check showed anything
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    assert True not in chat.typed and chat.typed[-1] is False
    assert chat.texts()[-1] == "Sam replied." and chat.cuts == [0]  # one fold: the card and the note together


@pytest.mark.made_up_rule
async def test_a_task_never_keeps_an_address_the_model_made_up(runner, agent, llm, chat, accounts):
    """SPEC §5.2 "Made-up addresses": an address in a task's instructions must be one the request was shown or
    the person sent; its checks may open only those (seen: a tracking address from memory, a 404, the task
    ended)."""
    fe = person(accounts)
    guessed = "https://www.fedex.com/en-gb/tracking?track-numbers=385413566727"
    sent = "https://parcel.example/t/AB123"
    llm.call(
        "task_create", title="Parcel", prompt=f"Open {guessed} and read the status.", every_minutes=60, until="never"
    )
    llm.call("task_create", title="Parcel", prompt=f"Open {sent} and read the status.", every_minutes=60, until="never")
    llm.answer("Watching it.")
    await agent.inbound(fe, f"tell me when {sent} says delivered")
    card = await chat.next()
    await agent.approval_reply(fe, card.card["id"], "approve")
    await settle(agent)
    refused = [m["content"] for m in llm.requests[-1]["messages"] if m.get("role") == "tool"][-2]
    assert refused.startswith(f"Not created: {guessed} isn't in anything you read")
    (entry,) = background.load(fe)
    assert entry["links"] == [sent] and background.links_of(fe, entry["id"]) == [sent]


async def test_the_person_edits_what_a_task_is_allowed(accounts):
    fe = person(accounts)
    background.save(fe, [task(grant={"open": ["royalmail.com"]})])
    assert background.listing(fe)[0]["grant"]["open"] == ["royalmail.com"]
    grant = {"open": [], "type": ["https://www.FedEx.com/x"], "email": ["Sam <sam@example.com>"], "drafts": True}
    assert actions.edit_background(fe, "abc123", {"grant": grant}, via="settings") == ""
    g = background.grant_of(fe, "abc123")
    assert g.type == {"fedex.com"} and g.open == set() and g.email == {"sam@example.com"} and g.drafts
    assert (
        background.listing(fe)[0]["allowed"][0] == "Type and fill in forms on fedex.com (searches, sign-ups, bookings)"
    )
    assert actions.edit_background(fe, "abc123", {"grant": {"type": ["not a site"]}}, via="settings")
    assert actions.edit_background(fe, "abc123", {"grant": {"email": ["a@b.com c@d.com"]}}, via="settings")
    assert background.grant_of(fe, "abc123").type == {"fedex.com"}  # a bad edit changes nothing
    # The person's own words: an address they write into what it checks is theirs to open.
    assert (
        actions.edit_background(fe, "abc123", {"prompt": "Read https://parcel.example/t/1 each time."}, via="x") == ""
    )
    assert background.links_of(fe, "abc123") == ["https://parcel.example/t/1"]


async def test_a_page_that_didnt_load_waits_for_the_next_check():
    assert "A page that didn't load or that you couldn't find isn't that: report waiting" in background.CHECK


async def test_check_result_is_only_for_a_check(agent, llm, chat):
    from .conftest import say

    if "check_result" not in agent.registry.names():
        background.register(agent.registry)
    llm.call("check_result", outcome="done", message="All done.")
    llm.answer("Here you go.")
    await say(agent, "hi")
    await settle(agent)
    assert chat.texts() == ["Here you go."]  # refused: a request the person watches answers them
    assert (
        "Only a background task's check"
        in [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    )
