# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The agent: memory approvals, context layers, commands, queueing (SPEC §3.3, §4, §5.4, §11.3–11.4)."""

import asyncio
import time
from types import SimpleNamespace

from aha_local import context, messages
from aha_local.tools.registry import Tool, ToolResult, params

from .conftest import person, say, settle


async def test_plain_answer_and_turn_recorded(agent, llm, chat, accounts):
    llm.answer("Hello Fe!")
    await say(agent, "hi")
    await settle(agent)
    assert chat.texts() == ["Hello Fe!"]
    turns = person(accounts).load_context()["turns"]
    assert [t["content"] for t in turns] == ["hi", "Hello Fe!"]


async def test_memory_save_needs_approval_and_lands_in_context(agent, llm, chat, accounts):
    llm.call("memory_add", text="Name is Felipe")
    llm.answer("Nice to meet you.")
    await say(agent, "I'm Felipe, call me Fe")
    ask = await chat.next()
    assert ask.card["kind"] == "memory" and ask.card["title"] == "Remember this?"
    assert [f["value"] for f in ask.card["fields"]] == ["Name is Felipe"]
    assert [b.label for b in ask.buttons] == ["Save", "Edit", "Don't"]
    await agent.approval_reply(person(accounts), ask.card["id"], "approve", ["Prefers to be called Fe"])  # edited
    await settle(agent)
    assert [m["text"] for m in person(accounts).load_memory()] == ["Prefers to be called Fe"]
    tool_result = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "Prefers to be called Fe" in tool_result and "Carry on" in tool_result
    assert "edited" not in tool_result and "approv" not in tool_result  # the model can't tell a card was there

    llm.answer("Sure.")
    await say(agent, "thanks")
    await settle(agent)
    block = llm.requests[-1]["messages"][1]["content"]
    assert "Prefers to be called Fe" in block and block.startswith("<context>")


async def test_declined_memory_is_not_saved(agent, llm, chat, accounts):
    llm.call("memory_add", text="Likes spicy food")
    llm.answer("OK.")
    await say(agent, "I like spicy food")
    ask = await chat.next()
    await say(agent, "❌ Don't", button=ask.buttons[-1].id)
    await settle(agent)
    assert person(accounts).load_memory() == []


async def test_memories_are_per_account(agent, llm, chat, accounts):
    from aha_local.tools import memory

    fe = person(accounts)
    other = accounts.create("Ana")
    memory.add_fact(fe, "Prefers to be called Fe")
    llm.answer("Hi!")
    await agent.inbound(other, "hi")
    await settle(agent)
    assert "Prefers to be called Fe" not in llm.requests[-1]["messages"][1]["content"]


async def test_prefix_is_identical_across_accounts_and_days(accounts):
    import datetime as dt

    other = accounts.create("Ana")
    a = context.build(person(accounts), [], "Europe/London", "Home", 131072, now=dt.datetime(2026, 11, 20))
    b = context.build(other, [], "Europe/London", "Home", 131072, now=dt.datetime(2026, 11, 21))
    assert a[0] == b[0]  # the system prompt never changes
    assert "{{" not in a[0]["content"] and "2026" not in a[0]["content"]
    assert "20/11/2026, a Friday" in a[1]["content"] and "21/11/2026, a Saturday" in b[1]["content"]


async def test_tool_traffic_is_dropped_after_the_task(agent, llm, chat, accounts, registry):
    async def big(args, ctx):
        return ToolResult("PAGE " * 5000 + "Confirmed: Fri 27 Nov 14:00, ref FX12", kind="page")

    registry.add(Tool("big_page", "a page", params(), big))
    llm.call("big_page")
    llm.answer("Booked Flat Iron, 27/11 14:00, ref FX12.")
    await say(agent, "book it")
    await settle(agent)
    llm.answer("You're welcome.")
    await say(agent, "thanks")
    await settle(agent)
    sent = llm.requests[-1]["messages"]
    assert not any("PAGE PAGE" in str(m.get("content")) for m in sent)
    assert any("ref FX12" in str(m.get("content")) for m in sent)
    transcript = person(accounts).read_transcript()
    assert any(e["kind"] == "tool" and e["name"] == "big_page" for e in transcript)


def test_older_pages_are_shortened_within_a_long_task():
    task = [{"role": "user", "content": "go"}]
    for i in range(6):
        task.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"page {i} " + "x" * 40000, "_kind": "page"})
    out = context.shorten_task(task, budget=40000)
    kept = [m["content"][:6] for m in out if m["role"] == "tool"]
    assert out[-1]["content"].startswith("page 5") and len(out[-1]["content"]) > 40000
    assert out[1]["content"].startswith("[earlier page result, superseded]")
    assert all("_kind" not in m for m in out)
    assert kept


def test_a_long_tasks_start_stays_the_same_from_step_to_step():
    """Seen on evri.com: stubbing just enough of the oldest pages changed the middle each step, so the model
    server read ~75k tokens again every time (50 s a step). Past the budget, all but the newest two are stubs."""
    task = [{"role": "user", "content": "go"}]
    for i in range(8):
        task.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"page {i} " + "x" * 30000, "_kind": "page"})
    before = context.shorten_task(task, budget=40000)
    task.append({"role": "tool", "tool_call_id": "c8", "content": "page 8 " + "x" * 30000, "_kind": "page"})
    after = context.shorten_task(task, budget=40000)
    assert after[: len(before) - 2] == before[: len(before) - 2]  # only the end changed: the rest stays cached
    assert [m["content"][:6] for m in after[-2:]] == ["page 7", "page 8"]


async def test_each_task_leaves_its_topic_memory_and_a_new_topic_starts_from_memory(agent, llm, chat, accounts):
    """SPEC §4, §5.4: the topic's entry is written at the end of each task, no card; a task sees the turns of the
    topic going on, and finds other topics' entries with conversation_search."""
    acct = person(accounts)
    llm.topic_reply = "SAME: Evri parcel\nMEMORY:\n- Evri tracking H01ABC123, collect from 26/11/2026"
    llm.answer("It's at the Evri shop.")
    await say(agent, "where is my parcel?")
    await settle(agent)
    [entry] = acct.load_context()["topics"]
    assert entry["title"] == "Evri parcel" and "H01ABC123" in entry["text"]
    assert not [c for c in chat.sent if getattr(c, "buttons", None)]  # no card
    llm.answer("Tomorrow.")
    await say(agent, "and when can I collect it?")
    await settle(agent)
    assert "where is my parcel?" in str(llm.requests[-1]["messages"])
    going_on = (entry["from"], entry["ts"])
    msgs = context.build(acct, [], "Europe/London", "Home", 100_000, topic=going_on)
    assert "H01ABC123" not in msgs[1]["content"]  # the topic going on: its turns are shown, not its entry
    assert "where is my parcel?" in str(msgs)

    topic = (999, time.time() + 1)  # a new topic, after a break
    msgs = context.build(acct, [], "Europe/London", "Home", 100_000, topic=topic)
    assert "H01ABC123" not in str(msgs) and "where is my parcel?" not in str(msgs)  # neither in every request
    from aha_local.tools.conversation import conversation_search

    found = await conversation_search({"query": "evri tracking"}, SimpleNamespace(account=acct))
    assert "Evri parcel" in found.text and "H01ABC123" in found.text


async def test_commands(agent, llm, chat, accounts):
    """Two commands: stop and help. The rest is in the app's Settings (2026-11-26)."""
    await say(agent, "help")
    assert "**stop**" in chat.texts()[-1]
    assert "Open source (AGPL-3.0): https://github.com/axxlabs/aha-local-llm" in chat.texts()[-1]
    await say(agent, "stop")
    assert chat.texts()[-1] == "Nothing is running."
    llm.answer("You haven't saved anything yet.")
    await say(agent, "memories")  # no longer a command: a request like any other
    await settle(agent)
    assert chat.texts()[-1] == "You haven't saved anything yet."


async def test_messages_during_a_task_reach_the_running_task(agent, llm, chat, registry):
    gate = asyncio.Event()

    async def slow(args, ctx):
        await gate.wait()
        return ToolResult("done")

    registry.add(Tool("slow", "slow", params(), slow))
    llm.call("slow")
    llm.call("message_user", text="Halfway there.")
    llm.answer("first done")
    await say(agent, "first")
    await asyncio.sleep(0.05)
    await say(agent, "how is it going?")
    await say(agent, "also check Tuesday")
    assert chat.texts() == []  # no acknowledgement: "typing…" shows a reply is coming
    gate.set()
    await settle(agent)
    assert chat.texts() == ["Halfway there.", "first done"]
    news = [m["content"] for m in llm.requests[1]["messages"] if str(m["content"]).startswith("[New message")]
    assert len(news) == 2 and "also check Tuesday" in news[1]
    turns = person(agent.accounts).load_context()["turns"]
    assert "also check Tuesday" in turns[-2]["content"]  # the turn records everything the person said
    assert len(llm.requests) == 3  # no extra task: the running one took the messages


async def test_messages_after_the_last_step_join_the_request(agent, llm, chat, registry):
    """SPEC §3.3: written while the model wrote its answer, they get one more call, not a request of their own."""
    gate = asyncio.Event()
    real_chat = llm.chat

    async def slow_answer(messages, tools=None, max_tokens=4096, who=None, **kw):
        if len(llm.requests) == 0:
            await gate.wait()
        return await real_chat(messages, tools, max_tokens, who, **kw)

    llm.chat = slow_answer
    llm.answer("first done")
    llm.answer("all done")
    await say(agent, "first")
    await asyncio.sleep(0.05)
    for text in ("one", "two", "three"):
        await say(agent, text)
    gate.set()
    await settle(agent)
    assert chat.texts() == ["all done"]  # the draft wasn't sent
    msgs = llm.requests[-1]["messages"]
    assert msgs[-4] == {"role": "assistant", "content": "first done"}
    assert "wasn't sent" in msgs[-3]["content"] and msgs[-3]["content"].endswith("one")
    assert msgs[-1]["content"].endswith("three")
    [turn] = [t for t in agent.accounts.all()[0].load_context()["turns"] if t["role"] == "user"]
    assert turn["content"] == "first\n\none\n\ntwo\n\nthree"


async def test_cancel_words_stop_the_task(agent, llm, chat, registry):
    gate = asyncio.Event()

    async def slow(args, ctx):
        await gate.wait()
        return ToolResult("done")

    registry.add(Tool("slow", "slow", params(), slow))
    llm.call("slow")
    await say(agent, "first")
    await asyncio.sleep(0.05)
    await say(agent, "Cancel!")
    await settle(agent)
    assert chat.texts() == ["Stopped."]


async def test_typing_while_a_reply_is_owed_but_not_while_an_approval_waits(agent, llm, chat):
    """The computer is the only source of "working" (SPEC §3.3): it says so, and says when it isn't."""
    agent.typing_every = 0.02
    llm.call("memory_add", text="Wife is vegetarian")
    llm.answer("Noted.")
    await say(agent, "my wife is vegetarian")
    ask = await chat.next()
    during = len(chat.typed)
    await asyncio.sleep(0.1)  # waiting on the person: the computer keeps saying it isn't working
    assert chat.typed[during - 1] is False and set(chat.typed[during:]) <= {False}
    await say(agent, "ok", button=ask.buttons[0].id)
    await settle(agent)
    assert chat.typed[0] is True and chat.typed[-1] is False
    assert True in chat.typed[during:]  # answered: working again, said straight away
    assert chat.texts() == [ask.text, "Noted."]  # no "On it…", no "Busy…"


async def test_an_empty_answer_after_an_approval_sends_only_the_receipt(agent, llm, chat):
    llm.call("memory_add", text="Lives in Testville")
    llm.answer("")
    await say(agent, "I live in Testville")
    approval = await chat.next()
    await say(agent, approval.buttons[0].label, button=approval.buttons[0].id)
    await settle(agent)
    [saved] = person(agent.accounts).load_memory()
    assert chat.texts() == [approval.text, ""]  # no "…" after the save, no words the model didn't write
    assert chat.sent[-1].record == [f"Saved [{saved['id']}]: Lives in Testville"]  # what was done, by the code


async def test_typing_ends_before_the_answer_and_stays_off_while_the_topic_is_written(agent, llm, chat, accounts):
    agent.typing_every = 0.01
    gate = asyncio.Event()
    real_chat = llm.chat

    async def slow_topic(messages, tools=None, max_tokens=4096, who=None, **kw):
        if len(llm.requests) == 1:  # the title and memory entry, after the answer
            await gate.wait()
        return await real_chat(messages, tools, max_tokens, who, **kw)

    llm.chat = slow_topic
    llm.answer("Fine.")
    await say(agent, "hello")
    for _ in range(100):
        if chat.texts():
            break
        await asyncio.sleep(0.01)
    assert chat.typed[-1] is False  # off, sent before "Fine."
    count = len(chat.typed)
    await asyncio.sleep(0.05)
    assert len(chat.typed) == count  # no typing while the topic is written
    gate.set()
    await settle(agent)


async def test_a_messages_details_are_its_tasks_model_calls_and_tools(agent, llm, chat, accounts):
    """A request's Details (SPEC §3.8): each model call, with its thinking and timing, and each tool, tagged by task."""
    from aha_local.e2e.channel import trace
    from aha_local.llm import Reply

    llm.call("current_time")
    llm.script.append(Reply("It's noon.", reasoning="The tool said 12:00.", ms=850, timings={"prompt_n": 30}))
    await say(agent, "what time is it?")
    await settle(agent)
    records = person(accounts).read_transcript()
    model = [r for r in records if r["kind"] == "model"]
    assert [r["calls"][-1]["name"] if r["calls"] else r["text"] for r in model] == ["current_time", "It's noon."]
    assert model[1]["reasoning"] == "The tool said 12:00." and model[1]["ms"] == 850
    task = model[0]["task"]
    assert all(r.get("task") == task for r in records if r["kind"] in ("model", "tool", "out", "answer"))
    from aha_local.accounts import TASK

    assert TASK.get() is None  # only inside the task
    sent = {"kind": "item", "item": {"t": "msg", "role": "aha", "seq": 7, "text": "It's noon."}, "task": task}
    events = trace([*records, sent], 7)
    assert [e["kind"] for e in events] == ["model", "tool", "model", "answer", "item"]
    assert "task" not in events[0] and events[-1]["text"] == "It's noon."
    assert trace(records, 8) == []  # not a task's message
    assert len(trace([*records, sent], 7, budget=600)) == 5  # shortened to fit, never dropped


async def test_whoever_has_a_task_running_hears_when_aha_restarts(accounts):
    """A task cut short by a restart isn't a mystery; nobody else is told (SPEC §3.3)."""
    from types import SimpleNamespace

    from aha_local.service import Service

    fe, hb = person(accounts), accounts.create("Hb")
    accounts.create("Idle")
    sent = []

    async def send(account, text, open=""):
        if account is hb:
            raise RuntimeError("offline")  # one failing doesn't stop the others
        sent.append((account.name, text))

    running = {fe.address: object(), hb.address: object()}  # Idle has nothing running
    service = SimpleNamespace(accounts=accounts, agent=SimpleNamespace(send=send, running=running))
    await Service.announce_restart(service)
    assert sent == [(fe.name, messages.RESTARTING)]


async def test_the_model_is_one_at_a_time_and_a_waiting_person_is_told_they_are_queued():
    """SPEC §3.3: while another person's request runs, the app shows "waiting its turn", not "working"."""
    from aha_local.llm import LLM, Slots

    llm = LLM("http://llm", "m")
    llm.slots = Slots(0, {})  # no server to ask for its slots here
    started, release = asyncio.Event(), asyncio.Event()

    async def post(body):
        started.set()
        await release.wait()
        return {"choices": [{"message": {"content": "ok"}}]}

    llm._post = post
    first = asyncio.create_task(llm.chat([], who="fe"))
    await started.wait()
    second = asyncio.create_task(llm.chat([], who="hb"))
    await asyncio.sleep(0)
    assert llm.queued("hb") and not llm.queued("fe")  # hb waits behind fe
    release.set()
    await asyncio.gather(first, second)
    assert not llm.queued("hb") and llm.holder is None and not llm.waiting


async def test_typing_says_queued_while_the_task_waits_for_another_persons_request(agent, llm, chat, accounts):
    fe = person(accounts)
    llm.queued_for.add(fe.address)
    agent.typing_every = 0.01
    real_chat = llm.chat

    async def slow(messages, tools=None, max_tokens=4096, who=None, **kw):
        await asyncio.sleep(0.05)
        return await real_chat(messages, tools, max_tokens, who, **kw)

    llm.chat = slow
    llm.answer("done")
    await say(agent, "hello")
    await settle(agent)
    assert "queued" in chat.typed and chat.typed[-1] is False


async def test_by_default_cards_and_tasks_wait_for_the_person_however_long(agent, llm, chat, accounts):
    """SPEC §3.3, §5.3: no card expiry and no task time limit; the step limit stops loops."""
    llm.call("memory_add", text="Name is Felipe")
    llm.answer("Saved.")
    await say(agent, "I'm Felipe")
    ask = await chat.next()
    await asyncio.sleep(0.3)  # the person is away
    assert agent.running
    await say(agent, "✅ Save", button=ask.buttons[0].id)
    await settle(agent)
    assert chat.texts()[-1] == "Saved."


async def test_stop_drops_everything_that_was_waiting_for_the_task(agent, llm, chat, accounts):
    """Seen in review: Stop dropped the waiting messages but not a waiting photo, which then went with the next,
    unrelated request. One session per person, reset as a whole."""
    from aha_local.items import Image

    fe = person(accounts)
    release = asyncio.Event()

    async def slow(args, ctx):
        await release.wait()
        return ToolResult("done")

    agent.registry.add(Tool("slow", "", params(), slow))
    llm.call("slow")
    await agent.inbound(fe, "do the slow thing")
    await asyncio.sleep(0.05)
    await agent.inbound(fe, "", Image(mime="image/jpeg", b64="/9j/AAAA"))  # a photo while it runs
    assert agent.session(fe).photos
    await agent.stop(fe)
    await settle(agent)
    assert not agent.session(fe).photos and not agent.session(fe).inbox and not agent.running
    llm.answer("Hello.")
    await agent.inbound(fe, "hi")
    await settle(agent)
    last = llm.requests[-1]["messages"]
    assert "image_url" not in str(last)  # the stopped task's photo didn't come along


async def test_the_topic_memory_is_asked_at_the_end_of_the_persons_cached_prompt(agent, llm, chat, accounts):
    """SPEC §3.2, §5.4: the task's prompt is already read in the person's slot; a prompt of its own re-reads it."""
    acct = person(accounts)
    llm.topic_reply = "SAME: Greeting\nMEMORY: said hello"
    llm.answer("Fine.")
    await say(agent, "hello")
    await settle(agent)
    task_request, asked = llm.requests[-1]["messages"], llm.titles[-1]
    assert asked[: len(task_request)] == task_request  # the same start: cached
    assert asked[len(task_request)] == {"role": "assistant", "content": "Fine."}
    assert "MEMORY:" in asked[-1]["content"]
    assert acct.load_context()["topics"][0]["text"] == "said hello"


async def test_tool_use_is_said_as_it_happens_once_per_run(agent, llm, chat):
    """SPEC §7.4 "Tools used": from the task's own calls, before each runs; the time isn't shown."""
    llm.call("conversation_search", query="parcel")
    llm.call("conversation_search", query="evri")
    llm.call("current_time")
    llm.call("show_screenshot")
    llm.answer("Done.")
    await say(agent, "where is my parcel?")
    await settle(agent)
    assert chat.tools == ["conversation history", "screenshot"]
