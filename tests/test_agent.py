"""The agent: memory approvals, context layers, commands, queueing, documents (SPEC §3.3, §4, §5.4, §11.3–11.4)."""

import asyncio

from aha_local import context
from aha_local.tools.registry import Tool, ToolResult, params

from .conftest import ADDRESS, said, settle


async def test_plain_answer_and_turn_recorded(agent, llm, chat, accounts):
    llm.answer("Hello Fe!")
    await agent.inbound(said("hi"))
    await settle(agent)
    assert chat.texts() == ["Hello Fe!"]
    turns = accounts.get(ADDRESS).load_context()["turns"]
    assert [t["content"] for t in turns] == ["hi", "Hello Fe!"]


async def test_memory_save_needs_approval_and_lands_in_context(agent, llm, chat, accounts):
    llm.call("memory_add", text="Name is Felipe")
    llm.answer("Nice to meet you.")
    await agent.inbound(said("I'm Felipe, call me Fe"))
    ask = await chat.next()
    assert ask.text == "🧠 *Remember this?*\n_Name is Felipe_"
    assert [b.label for b in ask.buttons] == ["✅ Save", "✏️ Edit", "❌ Don't"]
    await agent.inbound(said("✏️ Edit", button=ask.buttons[1].id))
    assert (await chat.next()).text == "Send the corrected value."
    await agent.inbound(said("Prefers to be called Fe"))
    again = await chat.next()
    assert "_Prefers to be called Fe_" in again.text
    await agent.inbound(said("✅ Save", button=again.buttons[0].id))
    await settle(agent)
    assert [m["text"] for m in accounts.get(ADDRESS).load_memory()] == ["Prefers to be called Fe"]
    tool_result = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "The user edited it" in tool_result and "Carry on" in tool_result

    llm.answer("Sure.")
    await agent.inbound(said("thanks"))
    await settle(agent)
    block = llm.requests[-1]["messages"][1]["content"]
    assert "Prefers to be called Fe" in block and block.startswith("<context>")


async def test_declined_memory_is_not_saved(agent, llm, chat, accounts):
    llm.call("memory_add", text="Likes spicy food")
    llm.answer("OK.")
    await agent.inbound(said("I like spicy food"))
    ask = await chat.next()
    await agent.inbound(said("❌ Don't", button=ask.buttons[-1].id))
    await settle(agent)
    assert accounts.get(ADDRESS).load_memory() == []


async def test_memories_are_per_account(agent, llm, chat, accounts):
    from aha_local.protocol import Cert
    from aha_local.tools import memory

    other = "sim:+447900000002"
    accounts.upsert(other, cert=Cert(address=other, instance="AAAA", issued=1, expires=2**40, sig="AAAA"))
    memory.add_fact(accounts.get(ADDRESS), "Prefers to be called Fe")
    llm.answer("Hi!")
    await agent.inbound(said("hi", sender=other))
    await settle(agent)
    assert "Prefers to be called Fe" not in llm.requests[-1]["messages"][1]["content"]


async def test_prefix_is_identical_across_accounts_and_days(accounts):
    import datetime as dt

    from aha_local.protocol import Cert

    other = accounts.upsert(
        "sim:+447900000002", cert=Cert(address="sim:+447900000002", instance="A", issued=1, expires=2**40, sig="A")
    )
    a = context.build(accounts.get(ADDRESS), [], "Europe/London", "Home", 131072, now=dt.datetime(2026, 11, 20))
    b = context.build(other, [], "Europe/London", "Home", 131072, now=dt.datetime(2026, 11, 21))
    assert a[0] == b[0]  # the system prompt never changes
    assert "{{" not in a[0]["content"] and "2026" not in a[0]["content"]
    assert "Friday 20 November 2026" in a[1]["content"] and "Saturday 21 November 2026" in b[1]["content"]


async def test_tool_traffic_is_dropped_after_the_task(agent, llm, chat, accounts, registry):
    async def big(args, ctx):
        return ToolResult("PAGE " * 5000 + "Confirmed: Fri 27 Nov 14:00, ref FX12", kind="page")

    registry.add(Tool("big_page", "a page", params(), big))
    llm.call("big_page")
    llm.answer("Booked Flat Iron, Fri 27 Nov 14:00, ref FX12.")
    await agent.inbound(said("book it"))
    await settle(agent)
    llm.answer("You're welcome.")
    await agent.inbound(said("thanks"))
    await settle(agent)
    sent = llm.requests[-1]["messages"]
    assert not any("PAGE PAGE" in str(m.get("content")) for m in sent)
    assert any("ref FX12" in str(m.get("content")) for m in sent)
    transcript = accounts.get(ADDRESS).read_transcript()
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


async def test_summary_folding_happens_between_tasks(agent, llm, chat, accounts):
    acct = accounts.get(ADDRESS)
    conv = {
        "summary": "",
        "turns": [{"role": r, "content": "word " * 3000, "ts": "2026-11-01 10:00"} for r in ("user", "assistant") * 10],
    }
    acct.save_context(conv)
    llm.answer("Fine.")
    llm.answer("- 1 Nov: talked a lot")  # the summary call
    await agent.inbound(said("hello"))
    await settle(agent)
    for _ in range(50):
        if acct.load_context()["summary"]:
            break
        await asyncio.sleep(0.01)
    new = acct.load_context()
    assert new["summary"] == "- 1 Nov: talked a lot"
    assert context.estimate(new["turns"]) <= context.TURNS_BUDGET
    assert new["turns"][-1]["content"] == "Fine."


async def test_commands(agent, llm, chat, accounts):
    await agent.inbound(said("help"))
    assert "Commands" in chat.texts()[-1]
    await agent.inbound(said("memories"))
    assert chat.texts()[-1] == "I don't remember anything about you yet."
    await agent.inbound(said("stop"))
    assert chat.texts()[-1] == "Nothing is running."
    llm.answer("a")
    await agent.inbound(said("q"))
    await settle(agent)
    await agent.inbound(said("forget last"))
    assert accounts.get(ADDRESS).load_context()["turns"] == []


async def test_messages_during_a_task_reach_the_running_task(agent, llm, chat, registry):
    gate = asyncio.Event()

    async def slow(args, ctx):
        await gate.wait()
        return ToolResult("done")

    registry.add(Tool("slow", "slow", params(), slow))
    llm.call("slow")
    llm.call("message_user", text="Halfway there.")
    llm.answer("first done")
    await agent.inbound(said("first"))
    await asyncio.sleep(0.05)
    await agent.inbound(said("how is it going?"))
    await agent.inbound(said("also check Tuesday"))
    assert chat.texts() == ["Got it, I'll take that into account."]  # one acknowledgement for the batch
    gate.set()
    await settle(agent)
    assert chat.texts()[1:] == ["Halfway there.", "first done"]
    news = [m["content"] for m in llm.requests[1]["messages"] if str(m["content"]).startswith("[New message")]
    assert len(news) == 2 and "also check Tuesday" in news[1]
    turns = agent.accounts.get(ADDRESS).load_context()["turns"]
    assert "also check Tuesday" in turns[-2]["content"]  # the turn records everything the person said
    assert len(llm.requests) == 3  # no extra task: the running one took the messages


async def test_messages_after_the_last_step_become_one_follow_up(agent, llm, chat, registry):
    gate = asyncio.Event()
    real_chat = llm.chat

    async def slow_answer(messages, tools=None, max_tokens=4096):
        if len(llm.requests) == 0:
            await gate.wait()
        return await real_chat(messages, tools, max_tokens)

    llm.chat = slow_answer
    llm.answer("first done")
    llm.answer("follow-up done")
    await agent.inbound(said("first"))
    await asyncio.sleep(0.05)
    for text in ("one", "two", "three"):
        await agent.inbound(said(text))
    gate.set()
    await settle(agent)
    assert chat.texts() == ["Got it, I'll take that into account.", "first done", "follow-up done"]
    last_user = [m for m in llm.requests[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert last_user == "one\n\ntwo\n\nthree"


async def test_cancel_words_stop_the_task(agent, llm, chat, registry):
    gate = asyncio.Event()

    async def slow(args, ctx):
        await gate.wait()
        return ToolResult("done")

    registry.add(Tool("slow", "slow", params(), slow))
    llm.call("slow")
    await agent.inbound(said("first"))
    await asyncio.sleep(0.05)
    await agent.inbound(said("Cancel!"))
    await settle(agent)
    assert chat.texts() == ["Stopped."]


async def test_on_it_after_a_while(agent, llm, chat, registry, config):
    object.__setattr__(config, "on_it_after", 0.05)

    async def slow(args, ctx):
        await asyncio.sleep(0.2)
        return ToolResult("ok")

    registry.add(Tool("slow", "slow", params(), slow))
    llm.call("slow")
    llm.answer("done")
    await agent.inbound(said("go"))
    await settle(agent)
    assert chat.texts() == ["On it…", "done"]


async def test_unpaired_addresses_are_ignored(agent, chat):
    await agent.inbound(said("hi", sender="sim:+447900000099"))
    assert chat.sent == []


async def test_documents_search_and_read(agent, llm, chat, config):
    (config.documents_mount / "home").mkdir()
    (config.documents_mount / "home" / "appliances.md").write_text(
        "# Appliances\n\nWashing machine LG F4V5, warranty until 2027-08-02.\n\nFridge Bosch, warranty 2026."
    )
    (config.documents_mount / "notes.txt").write_text("Plant watering: ficus weekly.")
    llm.call("documents_search", query="washing machine warranty")
    llm.call("documents_read", path="../../etc/passwd")
    llm.answer("Until 2 Aug 2027.")
    await agent.inbound(said("when does the washing machine warranty end?"))
    await settle(agent)
    tools = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "[home/appliances.md]" in tools[0]["content"] and "2027-08-02" in tools[0]["content"]
    assert "no such document" in tools[1]["content"]


async def test_no_on_it_after_an_approval(agent, llm, chat, config):
    object.__setattr__(config, "on_it_after", 0.1)
    llm.call("memory_add", text="Wife is vegetarian")
    llm.answer("Noted.")
    await agent.inbound(said("my wife is vegetarian"))
    ask = await chat.next()
    await asyncio.sleep(0.2)
    await agent.inbound(said("ok", button=ask.buttons[0].id))
    await settle(agent)
    assert "On it…" not in chat.texts()
