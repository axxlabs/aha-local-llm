"""A scripted model, a recorded chat and a paired account, so the agent can be tested without a GPU."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from aha_local.accounts import Accounts
from aha_local.agent import Agent
from aha_local.config import Config
from aha_local.llm import Reply, ToolCall
from aha_local.protocol import Cert, In, Out
from aha_local.tools import conversation, documents, memory
from aha_local.tools.registry import Registry

ADDRESS = "sim:+447900000001"


class FakeLLM:
    """Returns scripted replies in order and records every request."""

    def __init__(self) -> None:
        self.script: list[Reply] = []
        self.requests: list[dict] = []
        self.lock = asyncio.Lock()

    def answer(self, text: str) -> None:
        self.script.append(Reply(text))

    def call(self, name: str, **args) -> None:
        self.script.append(Reply("", [ToolCall(f"c{len(self.script)}", name, json.dumps(args))]))

    async def chat(self, messages, tools=None, max_tokens=4096) -> Reply:
        self.requests.append({"messages": messages, "tools": tools})
        await asyncio.sleep(0)
        return self.script.pop(0) if self.script else Reply("(script ended)")


class Chat:
    def __init__(self) -> None:
        self.sent: list[Out] = []
        self.read = 0  # messages already returned by next()

    async def send(self, frame: Out) -> None:
        self.sent.append(frame)

    async def next(self, timeout: float = 3) -> Out:
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
    (tmp_path / "docs").mkdir()
    return Config(
        gateway_url="unix:/nowhere",
        gateway_pin="x",
        data_dir=tmp_path / "data",
        documents_mount=tmp_path / "docs",
        on_it_after=30,
        approval_expiry=5,
        context_tokens=131072,
    )


@pytest.fixture
def accounts(config) -> Accounts:
    accts = Accounts(config.data_dir)
    accts.upsert(ADDRESS, cert=Cert(address=ADDRESS, instance="AAAA", issued=1, expires=2**40, sig="AAAA"), name="Fe")
    return accts


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    for module in (memory, documents, conversation):
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
    return Agent(config, accounts, llm, registry, chat.send)


def said(text: str = "", button: str | None = None, sender: str = ADDRESS) -> In:
    return In(id=f"m{time.monotonic_ns()}", sender=sender, text=text, button=button)


async def settle(agent: Agent, timeout: float = 3) -> None:
    """Wait until no task is running."""
    deadline = time.monotonic() + timeout
    while agent.running and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not agent.running, "a task is still running"
