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
from ..approvals import MASK, Approvals
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
    # What it changed, in one line, when it did (sent, saved, clicked, typed): kept in the request's record
    # (SPEC §4 "What a request did"). Empty: it only read.
    did: str = ""


MAX_DID = 300  # characters of one line of a request's record
MIN_SECRET = 3  # shorter values aren't masked in results: they'd blank out ordinary text


def redact(text: str, secrets: list[str]) -> str:
    """Hide what the person typed on a sign-in card, wherever it shows up again (SPEC §5.8)."""
    for s in sorted({s for s in secrets if len(s) >= MIN_SECRET}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return text


def redacted(value: Any, secrets: list[str]) -> Any:
    """A record with every string in it redacted (for the transcript)."""
    if not secrets:
        return value
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [redacted(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: redacted(v, secrets) for k, v in value.items()}
    return value


@dataclass
class TaskState:
    """What tools may remember during one run: a request the person made, or one background task's check
    (`ctx.task` in the tools). "Task" alone, elsewhere, means one of those runs; a background task (tasks.json)
    is always called that."""

    opened_sites: set[str] = field(default_factory=set)  # sites opened in the task (SPEC §5.2)
    last_snapshot: str = ""  # the latest page snapshot, to label approvals from the page itself
    page_url: str = ""  # the current page's address, from the browser
    ref_lines: dict[str, str] = field(default_factory=dict)  # elements seen on the current page: ref → its line
    page_parts: list[str] = field(default_factory=list)  # the current page as the model reads it, part by part
    ran: set[str] = field(default_factory=set)  # tools that ran without error in this task (set by the registry)
    # Values the person typed on a sign-in card: never shown again. The registry hides them in every tool result,
    # and the agent in every transcript record of the task (SPEC §5.8): tools add them, nothing else reads them.
    secrets: list[str] = field(default_factory=list)
    sign_in_page: str = ""  # the site, when the latest page is a sign-in form (SPEC §3.4 "Signing in")
    login_tried: bool = False  # browser_login ran in this task (whatever happened)
    saved_login_failed: set[str] = field(default_factory=set)  # hosts whose saved login led back to a form
    challenge_page: str = ""  # the site, when the latest page shows an "are you human?" check
    human_check_offered: bool = False  # a check card was shown in this task
    human_check_asked: bool = False  # browser_human_check was refused once with no check found (it asks again)
    # Addresses shown to the task (links on pages, search results, the person's messages): opening one of these
    # exactly isn't the model writing an address (SPEC §5.2 rule 7).
    seen_urls: set[tuple] = field(default_factory=set)
    banners_tried: set[tuple[str, str]] = field(default_factory=set)  # cookie banners closed: (page, button)
    # What the person ticked "Always allow for this task" on (tools/guard.allow_key): no card for it again in this
    # run; for a background task's check (`background`: its id) the task keeps it, for every check (SPEC §5.3).
    allowed: set[str] = field(default_factory=set)
    background: str = ""
    # What the task changed, one line per action that worked, set by the registry from each result's `did`: kept
    # with the request's answer, so a later request knows, even when this one stopped before saying so (SPEC §4).
    record: list[str] = field(default_factory=list)
    # A background task's check (SPEC §3.4): where its check_result call puts {"outcome", "message"}. None: this
    # isn't a check, and check_result refuses.
    report: dict | None = None


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
            result = await tool.handler(args, ctx)
        except Exception as e:  # a failing tool must not end the task; the model can adjust
            log.exception("tool %s failed", name)
            return ToolResult(f"{name} failed: {type(e).__name__}: {str(e)[:300]}", error=True)
        if ctx.task.secrets:
            result.text = redact(result.text, ctx.task.secrets)
        if not result.error:
            ctx.task.ran.add(name)
            if line := " ".join(result.did.split()):
                ctx.task.record.append(redact(f"{name}: {line}", ctx.task.secrets)[:MAX_DID])
        return result
