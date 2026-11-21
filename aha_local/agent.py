# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The agent loop (SPEC §3.3): one continuous chat per account, tasks, approvals, commands."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from typing import Protocol

from . import checks, context, messages
from .accounts import Account, Accounts
from .approvals import Approvals
from .config import Config
from .items import Image
from .llm import LLM, ModelUnavailable
from .tools import memory
from .tools.registry import Registry, TaskState, ToolContext

log = logging.getLogger("aha.agent")


class AgentOut(Protocol):
    """Where the agent's output goes: the end-to-end channel to the person's devices (e2e/channel.py)."""

    async def message(self, account: Account, text: str, image: Image | None = None) -> None: ...
    async def typing(self, account: Account, on: bool) -> None: ...
    async def card(self, account: Account, card: dict) -> None: ...
    async def state(self, account: Account, approval_id: str, state: str) -> None: ...
    async def text(self, account: Account, text: str) -> None: ...


TYPING_EVERY = 4.0  # seconds between `typing` frames while a reply is owed (SPEC §3.3)
STOP_WORDS = {"stop", "cancel", "stop it", "stop that"}
NEW_MESSAGE = "[New message from the user while you were working]\n{text}"


class _Counted:
    """The approvals' output, counted as "the person heard from this task" (so no stray "…" follows)."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    async def card(self, account: Account, card: dict) -> None:
        self.agent.sent[account.address] += 1
        await self.agent.out.card(account, card)

    async def state(self, account: Account, approval_id: str, state: str) -> None:
        await self.agent.out.state(account, approval_id, state)

    async def text(self, account: Account, text: str) -> None:
        self.agent.sent[account.address] += 1
        await self.agent.out.text(account, text)


class ApprovalExpired(Exception):
    pass


class Agent:
    def __init__(
        self, config: Config, accounts: Accounts, llm: LLM, registry: Registry, out: AgentOut, browser=None
    ) -> None:
        self.config = config
        self.accounts = accounts
        self.llm = llm
        self.registry = registry
        self.out = out
        self.browser = browser
        self.approvals = Approvals(_Counted(self), config.approval_expiry)
        self.running: dict[str, asyncio.Task] = {}
        # Messages that arrive while the account's task runs: the task reads them before its next model call.
        self.inbox: dict[str, list[str]] = defaultdict(list)
        self.sent: dict[str, int] = defaultdict(int)  # messages sent per account, to know if they've heard back
        self.typing_every = TYPING_EVERY

    # ---- outbound

    async def send(self, account: Account, text: str, image: Image | None = None) -> None:
        self.sent[account.address] += 1
        account.transcript({"kind": "out", "text": text, "image": bool(image)})
        await self.out.message(account, text[:20_000], image)

    # ---- inbound

    async def inbound(self, account: Account, text: str) -> None:
        """A message the person typed (already shown on their devices by the channel)."""
        text = text.strip()
        account.transcript({"kind": "user", "text": text})
        command = text.lower().rstrip(".!")

        if command in STOP_WORDS:
            await self.stop(account)
            return
        if await self.approvals.reply_text(account, text):
            return
        if command == "help":
            await self.send(account, messages.HELP.format(source=self.config.source_url))
            return
        if command == "memories":
            items = account.load_memory()
            await self.send(
                account,
                "What I remember about you:\n" + memory.render(items)
                if items
                else "I don't remember anything about you yet.",
            )
            return
        if command == "forget last":
            context.forget_last(account)
            await self.send(account, messages.FORGOT_LAST)
            return
        if not text:
            return
        if account.address in self.running:
            self.inbox[account.address].append(text)  # the running task reads it; "typing…" already shows
            return
        self.running[account.address] = asyncio.create_task(self.run(account, text))

    async def approval_reply(
        self, account: Account, approval_id: str, decision: str, values: list | None = None, note: str = ""
    ) -> None:
        account.transcript({"kind": "approval_reply", "id": approval_id, "decision": decision})
        await self.approvals.answer(account, approval_id, decision, values, note)

    async def stop(self, account: Account) -> None:
        task = self.running.get(account.address)
        self.inbox.pop(account.address, None)
        if task is None:
            await self.send(account, messages.NOTHING_TO_STOP)
            return
        task.cancel()
        await self.send(account, messages.STOPPED)

    # ---- tasks

    async def run(self, account: Account, text: str) -> None:
        held = self.browser.hold(account.id) if self.browser else contextlib.nullcontext()
        try:
            with held:
                await self._run(account, text)
        finally:
            self.running.pop(account.address, None)

    async def _run(self, account: Account, text: str) -> None:
        while True:
            await self._task(account, text)
            # Messages that came in after the task's last model call: one follow-up task for all of them.
            pending = self.inbox.pop(account.address, [])
            if not pending:
                break
            text = "\n\n".join(pending)

    async def _typing(self, account: Account) -> None:
        """While a reply is owed: "typing…", refreshed, except while the person owes us an approval answer."""
        while True:
            if self.approvals.pending_for(account.address) is None:
                await self._typing_frame(account, True)
            await asyncio.sleep(self.typing_every)

    async def _stop_typing(self, account: Account, typing: asyncio.Task) -> None:
        typing.cancel()
        await self._typing_frame(account, False)

    async def _typing_frame(self, account: Account, on: bool) -> None:
        try:
            await self.out.typing(account, on)
        except Exception:
            log.warning("couldn't send a typing message")

    async def _task(self, account: Account, text: str) -> None:
        sent_before = self.sent[account.address]
        typing = asyncio.create_task(self._typing(account))
        try:
            answer, checked, text = await asyncio.wait_for(
                self.solve(account, text), timeout=self.config.max_task_seconds
            )
        except asyncio.CancelledError:
            account.transcript({"kind": "stopped"})
            await self._stop_typing(account, typing)
            raise
        except TimeoutError:
            answer, checked = messages.TOO_LONG, False
        except ApprovalExpired:
            answer, checked = messages.APPROVAL_EXPIRED, False
        except ModelUnavailable as e:
            log.error("model unavailable: %s", e)
            answer, checked = messages.MODEL_DOWN, False
        except Exception:
            log.exception("task failed")
            answer, checked = messages.FAILED, False
        if not answer.strip():
            # After "✅ Save" there may be nothing to add; if they've heard nothing at all, say something.
            answer = "" if self.sent[account.address] > sent_before else "…"
        await self._stop_typing(account, typing)  # before the answer, so no refresh can land after it
        if answer:
            await self.send(account, answer)
        account.transcript({"kind": "answer", "text": answer})
        conv = context.record_turn(account, text, answer, checked)
        await self._fold(account, conv)

    async def solve(self, account: Account, text: str) -> tuple[str, bool, str]:
        """Returns the answer, whether it passed the checks, and everything the person said during the task."""
        task: list[dict] = [{"role": "user", "content": text}]

        async def send(message: str) -> None:
            await self.send(account, checks.chat_format(message))

        # The browser is reached only through this account's own context (SPEC §3.4).
        browser = self.browser.for_account(account.id) if self.browser else None
        ctx = ToolContext(account, self.approvals, self.config, TaskState(), browser, send)
        tools = self.registry.specs()
        last = ""
        retried = False
        memory_retried = False
        called: set[str] = set()  # tools that ran without error in this task
        # Where numbers in the answer may come from: what the person said or saved, earlier answers, and pages
        # opened in this task. Search snippets don't count (SPEC §3.5).
        conv = account.load_context()
        sources = [
            text,
            *(m["text"] for m in account.load_memory()),
            *(t["content"] for t in conv.get("turns", []) if t["role"] == "user" or t.get("checked")),
        ]
        for _ in range(self.config.max_model_calls):
            for extra in self.inbox.pop(account.address, []):
                task.append({"role": "user", "content": NEW_MESSAGE.format(text=extra)})
                sources.append(extra)
                text = f"{text}\n\n{extra}"
            msgs = context.build(
                account, task, self.config.timezone, self.config.household_name, self.config.context_tokens
            )
            reply = await self.llm.chat(msgs, tools)
            log.info("model call: %d prompt tokens, %d cached", reply.prompt_tokens, reply.cached_tokens)
            if not reply.tool_calls:
                if not memory_retried and checks.unbacked_memory_claim(text, reply.content or "", called):
                    memory_retried = True
                    log.info("draft answer claimed a memory change no tool made; asking the model to fix it")
                    task.append({"role": "assistant", "content": reply.content or ""})
                    task.append({"role": "user", "content": checks.MEMORY_RETRY})
                    continue
                missing = checks.unverified(checks.chat_format(reply.content or ""), sources)
                if missing and not retried:
                    # One more go: ask the model to confirm the numbers on a page, or drop them (SPEC §3.5).
                    retried = True
                    log.info("draft answer had %d unconfirmed number(s); asking the model to check", len(missing))
                    task.append({"role": "assistant", "content": reply.content or ""})
                    task.append({"role": "user", "content": checks.retry_prompt(missing)})
                    continue
                return (*finish(reply.content or "", sources), text)
            last = reply.content or last
            task.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                        for c in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                result = await self.registry.call(call.name, call.arguments, ctx)
                if result.kind == "expired":
                    raise ApprovalExpired
                account.transcript(
                    {
                        "kind": "tool",
                        "name": call.name,
                        "args": _short(call.arguments),
                        "result": result.text[:4000],
                        "error": result.error,
                    }
                )
                if not result.error:
                    called.add(call.name)
                if call.name not in UNSOURCED_TOOLS and not result.error:
                    sources.append(result.text)
                task.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result.text or "(no output)",
                        "_kind": result.kind,
                    }
                )
                for image in result.images:
                    task.append(
                        {
                            "role": "user",
                            "_kind": "image",
                            "content": [
                                {"type": "text", "text": f"[Screenshot returned by {call.name}]"},
                                {"type": "image_url", "image_url": {"url": f"data:{image.mime};base64,{image.b64}"}},
                            ],
                        }
                    )
        return (*finish(f"{messages.TOO_MANY_STEPS}\n{last}".strip(), sources), text)

    async def _fold(self, account: Account, conv: dict) -> None:
        """Between tasks only: fold the oldest turns into the summary (SPEC §4.4)."""
        folded = context.turns_to_fold(conv)
        if not folded:
            return
        try:
            reply = await self.llm.chat(context.fold_request(conv, folded), max_tokens=1500)
        except ModelUnavailable:
            return
        if reply.content:
            context.apply_fold(account, folded, reply.content)


UNSOURCED_TOOLS = {"web_search"}  # snippets are leads, not sources


def finish(answer: str, sources: list[str]) -> tuple[str, bool]:
    """App formatting, and a visible warning for numbers no opened page supports.

    Returns the answer and whether it passed: only answers that passed count as sources later.
    """
    answer = checks.chat_format(answer)
    missing = checks.unverified(answer, sources)
    if missing:
        log.info("answer had %d unconfirmed number(s)", len(missing))
        return f"{answer}\n\n{checks.warning(missing)}", False
    return answer, True


def _short(arguments: str) -> str:
    try:
        return json.dumps(json.loads(arguments), ensure_ascii=False)[:2000]
    except json.JSONDecodeError:
        return arguments[:2000]
