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
    full: str = field(default="", repr=False)  # a page before it was cut to fit, for the harness's own checks


@dataclass
class TaskState:
    """What tools may remember during one task."""

    opened_sites: set[str] = field(default_factory=set)  # sites opened in the task (SPEC §5.2)
    last_snapshot: str = ""  # the latest page snapshot, to label approvals from the page itself
    page_url: str = ""  # the current page's address, from the browser
    blank_page: bool = False  # the last page the model saw is a new window's blank page (SPEC §3.4)
    ref_lines: dict[str, str] = field(default_factory=dict)  # elements seen on the current page: ref → its line
    # The task holds private data (SPEC §5.2): set by the registry after any tool declared `private` ran, and by
    # the browser when a page of a signed-in site shows. Tools never set it themselves.
    private: bool = False
    secrets: list[str] = field(default_factory=list)  # values the person typed on a sign-in card: never shown again
    signed_in_sites: set[str] | None = None  # the sites the person signed in on (read once per task)
    sign_in_page: str = ""  # the site, when the latest page is a sign-in form (SPEC §3.4 "Signing in")
    login_tried: bool = False  # browser_login ran in this task (whatever happened)
    saved_login_failed: set[str] = field(default_factory=set)  # hosts whose saved login led back to a form
    challenge_page: str = ""  # the site, when the latest page shows an "are you human?" check
    human_check_offered: bool = False  # a check card was shown in this task
    # Addresses shown to the task (links on pages, search results, the person's messages): opening one of these
    # exactly isn't the model writing an address (SPEC §5.2 rule 7).
    seen_urls: set[tuple] = field(default_factory=set)
    banners_tried: set[tuple[str, str]] = field(default_factory=set)  # cookie banners closed: (page, button)


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
    private: bool = False  # what it returns is the person's private data: the task then holds it (SPEC §5.2)

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
            result = await tool.handler(args, ctx)
        except Exception as e:  # a failing tool must not end the task; the model can adjust
            log.exception("tool %s failed", name)
            return ToolResult(f"{name} failed: {type(e).__name__}: {str(e)[:300]}", error=True)
        if tool.private and not result.error:  # it read the person's private data (SPEC §5.2): declared, not set
            ctx.task.private = True
        return result
