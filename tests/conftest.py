# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""A scripted model, a recorded chat and a paired account, so the agent can be tested without a GPU."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aha_local.accounts import Accounts
from aha_local.agent import Agent
from aha_local.config import Config
from aha_local.llm import Reply, ToolCall
from aha_local.tools import conversation, mail, memory
from aha_local.tools.registry import Registry
from aha_local.vault import Vault


class FakeLLM:
    """Returns scripted replies in order and records every request."""

    model = "fake-model"

    def __init__(self) -> None:
        self.script: list[Reply] = []
        self.requests: list[dict] = []
        self.lock = asyncio.Lock()
        self.queued_for: set[str] = set()  # accounts whose request waits behind another's
        self.titles: list[list[dict]] = []  # the hidden title requests (SPEC §7.4 "Folding"), kept apart
        self.topic_reply = "Test title"  # the reply to them: a title, and maybe a memory entry (SPEC §5.4)

    def answer(self, text: str) -> None:
        self.script.append(Reply(text))

    def call(self, name: str, **args) -> None:
        self.script.append(Reply("", [ToolCall(f"c{len(self.script)}", name, json.dumps(args))]))

    def queued(self, who) -> bool:
        return who in self.queued_for

    def turn(self, who):  # one task at a time is tested on the real LLM (test_slots.py)
        return contextlib.nullcontext()

    def paused(self, who):
        return contextlib.nullcontext()

    def wrote(self, who) -> None:
        pass

    async def chat(self, messages, tools=None, max_tokens=4096, who=None, tool_choice="auto", thinking=True) -> Reply:
        if tool_choice == "none" and "NEW:" in str(messages[-1].get("content")):  # a title: not part of the script
            self.titles.append(messages)
            return Reply(self.topic_reply)
        self.requests.append({"messages": messages, "tools": tools})
        await asyncio.sleep(0)
        return self.script.pop(0) if self.script else Reply("(script ended)")


class Chat:
    """Records what the agent sends to the person (it implements agent.AgentOut, like the real channel).

    Messages and approval cards are kept in order as objects with `.text` and `.buttons` (each with `.id` and
    `.label`); a card's button ids are "ap-<approval id>-yes|edit|no", which `say(..., button=...)` turns back
    into an approval reply.
    """

    def __init__(self) -> None:
        self.sent: list[SimpleNamespace] = []
        self.read = 0  # messages already returned by next()
        self.typed: list[bool] = []  # typing on/off, in order
        self.states: list[tuple[str, str]] = []
        self.titles: list[tuple[int, str]] = []
        self.tools: list[str] = []  # tool lines, as they happened (SPEC §7.4 "Tools used")
        self.cuts: list[int] = []  # the folds Aha! made (a background task's note starts a topic)

    async def message(self, account, text, image=None, open="", task=None) -> None:
        self.sent.append(SimpleNamespace(text=text, buttons=[], image=image, card=None, open=open, task=task))

    async def cut(self, account) -> None:
        self.cuts.append(len(self.sent))  # where it folded: before the next message

    async def tool(self, account, label) -> None:
        self.tools.append(label)

    async def title(self, account, start, text, new=False) -> None:
        self.titles.append((start, text) if not new else (start, text, "new"))

    async def text(self, account, text) -> None:
        await self.message(account, text)

    async def typing(self, account, on, queued=False) -> None:
        self.typed.append("queued" if on and queued else on)

    async def card(self, account, card) -> None:
        icon = "🧠" if card["kind"] in ("memory", "forget") else "✋"
        lines = [f"{icon} {card['title']}"] + [f"{f['label']}: {f['value']}" for f in card["fields"]]
        actions = ["yes", "edit", "no"] if len(card["buttons"]) == 3 else ["yes", "no"]
        buttons = [
            SimpleNamespace(id=f"ap-{card['id']}-{a}", label=lbl)
            for a, lbl in zip(actions, card["buttons"], strict=True)
        ]
        image = card.get("image")
        self.sent.append(SimpleNamespace(text="\n".join(lines), buttons=buttons, image=image, card=card))

    async def state(self, account, approval_id, state) -> None:
        self.states.append((approval_id, state))

    async def next(self, timeout: float = 3) -> SimpleNamespace:
        """The next message not read yet, waiting for it if needed."""
        deadline = time.monotonic() + timeout
        while len(self.sent) <= self.read:
            if time.monotonic() > deadline:
                raise TimeoutError("no new message")
            await asyncio.sleep(0.01)
        self.read += 1
        return self.sent[self.read - 1]

    def texts(self) -> list[str]:
        return [o.text for o in self.sent]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        gateway_url="unix:/nowhere",
        gateway_pin="x",
        data_dir=tmp_path / "data",
        approval_expiry=5,
        context_tokens=131072,
    )


@pytest.fixture
def accounts(config) -> Accounts:
    accts = Accounts(Vault(config.data_dir, os.urandom(32)))
    accts.create("Fe")
    return accts


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    for module in (memory, mail, conversation):
        module.register(reg)
    return reg


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def chat() -> Chat:
    return Chat()


@pytest.fixture
def agent(config, accounts, llm, registry, chat) -> Agent:
    return Agent(config, accounts, llm, registry, chat)


def person(accounts: Accounts):
    return accounts.all()[0]


async def say(agent: Agent, text: str = "", button: str | None = None) -> None:
    """What the channel does with a device's message: a typed text, or a tap on an approval card's button."""
    account = person(agent.accounts)
    if button is not None:
        _, approval_id, action = button.split("-")
        decision = {"yes": "approve", "no": "decline", "edit": "edit"}[action]
        await agent.approval_reply(account, approval_id, decision)
    else:
        await agent.inbound(account, text)


async def settle(agent: Agent, timeout: float = 3) -> None:
    """Wait until no task is running (a background check included)."""
    deadline = time.monotonic() + timeout

    def running() -> bool:
        return any(s.task is not None for s in agent.sessions.values())

    while running() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not running(), "a task is still running"
