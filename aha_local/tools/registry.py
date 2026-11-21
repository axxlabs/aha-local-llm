# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Tool definitions and dispatch (SPEC §3.4).

Definitions are static and in a fixed order, identical for every request and account, so they stay inside
the cached prompt prefix (SPEC §4.2).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..accounts import Account
from ..approvals import Approvals
from ..config import Config
from ..items import Image

log = logging.getLogger("aha.tools")


@dataclass
class ToolResult:
    text: str
    images: list[Image] = field(default_factory=list)
    kind: str = "text"  # "page" results (browser snapshots) may be shortened later in a long task (SPEC §4.3)
    error: bool = False


@dataclass
class TaskState:
    """What tools may remember during one task."""

    approved_sites: set[str] = field(default_factory=set)
    last_snapshot: str = ""  # the latest page snapshot, to label approvals from the page itself
    read_mail: bool = False  # set by email_search/email_read: from then on, text going out is a card (SPEC §5.5)
    secrets: list[str] = field(default_factory=list)  # values the person typed on a sign-in card: never shown again
    signed_in: set[str] = field(default_factory=set)  # sites the person chose to stay signed in on, this task


@dataclass
class ToolContext:
    account: Account
    approvals: Approvals
    config: Config
    task: TaskState
    browser: Any = None  # tools.browser.AccountBrowser: this account's own browser context, if available
    send: Callable[..., Awaitable[None]] | None = None  # message the person now (text, image=None), mid-task


Handler = Callable[[dict, ToolContext], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def params(required: list[str] | None = None, **props: dict) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def string(description: str) -> dict:
    return {"type": "string", "description": description}


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def specs(self) -> list[dict]:
        return [self._tools[n].spec() for n in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)

    async def call(self, name: str, raw_args: str | dict, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"There is no tool called {name}.", error=True)
        try:
            args: Any = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except (json.JSONDecodeError, ValueError) as e:
            return ToolResult(f"The arguments for {name} weren't valid JSON ({e}).", error=True)
        try:
            return await tool.handler(args, ctx)
        except Exception as e:  # a failing tool must not end the task; the model can adjust
            log.exception("tool %s failed", name)
            return ToolResult(f"{name} failed: {type(e).__name__}: {str(e)[:300]}", error=True)
