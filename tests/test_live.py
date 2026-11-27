# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Live in the app (SPEC §7.4 "Details", "Live diagnostics"): a request's records as they happen, under one
Details line; the computer's load and temperatures in the header."""

import asyncio

from aha_local.diagnostics import Reader
from aha_local.tools.registry import Tool, ToolResult, params

from .conftest import person, say, settle


async def test_a_requests_records_go_live_under_one_details_line(agent, llm, chat):
    async def look(args, ctx):
        return ToolResult("a page")

    agent.registry.add(Tool("web_search", "", params(), look))
    llm.call("web_search")
    llm.call("web_search")
    llm.answer("Found it.")
    await say(agent, "find it")
    await settle(agent)
    assert chat.details == 1  # one line, however many steps
    kinds = [e["kind"] for e in chat.events]
    assert kinds == ["model", "tool", "model", "tool", "model"]
    assert chat.events[0]["calls"][0]["label"] == "web search"  # what it's starting, before the tool runs
    assert chat.events[1]["label"] == "web search" and chat.events[1]["result"] == "a page"
    assert chat.tools == ["web search"]
    assert chat.texts() == ["Found it."]  # the answer is still a message of its own


async def test_each_thing_done_is_in_the_chat_where_it_happened(agent, llm, chat, accounts):
    """SPEC §7.4 "What Aha! did": every tool call is a chat item of its own, as it happens, between the cards and
    messages around it, never moved to the end: a change in its own words, anything else as what it looked at."""

    async def act(args, ctx):
        return ToolResult("done", did="Sent a message on whatsapp.com")

    async def look(args, ctx):
        return ToolResult("a page")

    agent.registry.add(Tool("act", "", params(), act))
    agent.registry.add(Tool("look", "", params(), look))
    llm.call("look")
    llm.call("act")
    llm.call("memory_add", text="Likes tea")
    llm.answer("Sent.")
    await say(agent, "message her")
    card = await chat.next()
    while card.card is None:
        card = await chat.next()
    await say(agent, "ok", button=card.buttons[0].id)
    await settle(agent)
    [saved] = person(accounts).load_memory()
    assert [k for k, _ in chat.order] == ["act", "act", "card", "act", "msg"]
    assert chat.acts == ["Used look", "Sent a message on whatsapp.com", f"Saved [{saved['id']}]: Likes tea"]
    assert chat.order[-1] == ("msg", "Sent.")


async def test_a_plain_answer_has_its_details_too(agent, llm, chat):
    llm.answer("Hello.")
    await say(agent, "hi")
    await settle(agent)
    assert chat.details == 1 and [e["kind"] for e in chat.events] == ["model"]


def test_the_readings_come_from_the_hosts_counters(tmp_path):
    proc, sys = tmp_path / "proc", tmp_path / "sys"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal:       16777216 kB\nMemFree:  1 kB\nMemAvailable:    8388608 kB\n")
    mon = sys / "class" / "hwmon" / "hwmon2"
    mon.mkdir(parents=True)
    (mon / "name").write_text("k10temp\n")
    (mon / "temp1_input").write_text("54250\n")
    other = sys / "class" / "hwmon" / "hwmon0"
    other.mkdir(parents=True)
    (other / "name").write_text("nvme\n")
    (other / "temp1_input").write_text("39000\n")
    reader = Reader(proc, sys)
    reader._gpu = False  # no NVIDIA GPU here
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    first = asyncio.run(reader.read())
    assert first == {"t": "stats", "cpu": {"temp": 54}, "mem": {"used": 8.0, "total": 16.0}}  # no load yet
    (proc / "stat").write_text("cpu  175 0 175 850 0 0 0 0 0 0\n")  # 150 busy of 200 since
    assert asyncio.run(reader.read())["cpu"] == {"load": 75, "temp": 54}


def test_what_cant_be_read_is_left_out(tmp_path):
    reader = Reader(tmp_path / "none", tmp_path / "none")
    reader._gpu = False
    assert asyncio.run(reader.read()) == {"t": "stats"}


def test_the_gpus_load_is_the_average_since_the_last_reading(monkeypatch):
    """nvidia-smi's one figure is the last moment's: the model works in bursts, so it read 0% almost always."""
    import sys
    from types import SimpleNamespace

    def sample(ts, v):
        return SimpleNamespace(timeStamp=ts, sampleValue=SimpleNamespace(uiVal=v))

    fake = SimpleNamespace(
        NVML_GPU_UTILIZATION_SAMPLES=1,
        NVML_TEMPERATURE_GPU=0,
        nvmlDeviceGetSamples=lambda h, kind, since: (
            0,
            [s for s in (sample(10, 0), sample(20, 90), sample(30, 60)) if s.timeStamp > since],
        ),
        nvmlDeviceGetTemperature=lambda h, which: 61,
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(used=14 * 1024**3, total=16 * 1024**3),
        nvmlDeviceGetUtilizationRates=lambda h: SimpleNamespace(gpu=0),
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    reader = Reader()
    reader._gpu, reader._gpu_since = "handle", 5
    assert reader.gpu() == {"load": 50, "temp": 61, "mem": 14.0, "mem_total": 16.0}
    assert reader._gpu_since == 30  # the next reading counts only newer samples


def test_every_call_has_its_line_and_reads_never_show_what_they_found():
    from aha_local.messages import call_line

    assert call_line("web_search", {"query": "Flat Iron hours"}, False) == "Searched Google for “Flat Iron hours”"
    assert call_line("browser_navigate", {"url": "https://www.waitrose.com/"}, False) == "Opened www.waitrose.com/"
    assert call_line("email_read", {"id": "m1"}, False) == "Read an email"  # never what it said
    assert call_line("fetch_url", {"url": "https://x.example/a"}, True) == "Read x.example/a (didn't work)"
    assert call_line("something_new", {}, False) == "Used something new"
