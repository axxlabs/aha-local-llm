# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Live in the app (SPEC §7.4 "Details", "Live diagnostics"): a request's records as they happen, under one
Details line; the computer's load and temperatures in the header."""

import asyncio

from aha_local.diagnostics import Reader
from aha_local.tools.registry import Tool, ToolResult, params

from .conftest import say, settle


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
    reader._smi = None  # no NVIDIA GPU here
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    first = asyncio.run(reader.read())
    assert first == {"t": "stats", "cpu": {"temp": 54}, "mem": {"used": 8.0, "total": 16.0}}  # no load yet
    (proc / "stat").write_text("cpu  175 0 175 850 0 0 0 0 0 0\n")  # 150 busy of 200 since
    assert asyncio.run(reader.read())["cpu"] == {"load": 75, "temp": 54}


def test_what_cant_be_read_is_left_out(tmp_path):
    reader = Reader(tmp_path / "none", tmp_path / "none")
    reader._smi = None
    assert asyncio.run(reader.read()) == {"t": "stats"}
