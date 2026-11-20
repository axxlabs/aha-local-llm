"""The agent loop (SPEC §3.3): one continuous chat per account, tasks, approvals, commands."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from . import context, messages
from .accounts import Account, Accounts
from .approvals import Approvals
from .config import Config
from .llm import LLM, ModelUnavailable
from .protocol import Button, Image, In, Out
from .tools import memory
from .tools.registry import Registry, TaskState, ToolContext

log = logging.getLogger("aha.agent")

SendOut = Callable[[Out], Awaitable[None]]
COMMANDS = {"stop", "help", "memories", "forget last"}


class ApprovalExpired(Exception):
    pass


class Agent:
    def __init__(self, config: Config, accounts: Accounts, llm: LLM, registry: Registry, send_out: SendOut) -> None:
        self.config = config
        self.accounts = accounts
        self.llm = llm
        self.registry = registry
        self.send_out = send_out
        self.approvals = Approvals(self.send, config.approval_expiry)
        self.running: dict[str, asyncio.Task] = {}
        self.queued: dict[str, deque[str]] = defaultdict(deque)

    # ---- outbound

    async def send(
        self, account: Account, text: str, buttons: list[Button] | None = None, image: Image | None = None
    ) -> None:
        account.transcript(
            {"kind": "out", "text": text, "buttons": [b.label for b in buttons or []], "image": bool(image)}
        )
        await self.send_out(
            Out(id=uuid.uuid4().hex, to=account.address, text=text[:4096], buttons=buttons or [], image=image)
        )

    # ---- inbound

    async def inbound(self, frame: In) -> None:
        account = self.accounts.get(frame.sender)
        if account is None or account.cert is None:
            log.warning("message for an address this computer doesn't hold; ignored")
            return
        if frame.name and not account.name:
            self.accounts.upsert(account.address, name=frame.name)
        text = frame.text.strip()
        account.transcript({"kind": "user", "text": text, "button": frame.button})
        command = text.lower().rstrip(".!") if frame.button is None else ""

        if command == "stop":
            await self.stop(account)
            return
        if await self.approvals.reply(account, text, frame.button):
            return
        if frame.button is not None:
            await self.send(account, messages.APPROVAL_STALE)
            return
        if command == "help":
            await self.send(account, messages.HELP)
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
            self.queued[account.address].append(text)
            await self.send(account, messages.QUEUED)
            return
        self.running[account.address] = asyncio.create_task(self.run(account, text))

    async def stop(self, account: Account) -> None:
        task = self.running.get(account.address)
        self.queued.pop(account.address, None)
        if task is None:
            await self.send(account, messages.NOTHING_TO_STOP)
            return
        task.cancel()
        await self.send(account, messages.STOPPED)

    # ---- tasks

    async def run(self, account: Account, text: str) -> None:
        try:
            while True:
                await self._task(account, text)
                if not self.queued.get(account.address):
                    break
                text = self.queued[account.address].popleft()
        finally:
            self.running.pop(account.address, None)

    async def _task(self, account: Account, text: str) -> None:
        on_it = asyncio.create_task(self._on_it(account))
        try:
            if self.llm.lock.locked() and len(self.running) > 1:
                await self.send(account, messages.BUSY)
            answer = await asyncio.wait_for(self.solve(account, text), timeout=self.config.max_task_seconds)
        except asyncio.CancelledError:
            account.transcript({"kind": "stopped"})
            raise
        except TimeoutError:
            answer = messages.TOO_LONG
        except ApprovalExpired:
            answer = messages.APPROVAL_EXPIRED
        except ModelUnavailable as e:
            log.error("model unavailable: %s", e)
            answer = messages.MODEL_DOWN
        except Exception:
            log.exception("task failed")
            answer = messages.FAILED
        finally:
            on_it.cancel()
        await self.send(account, answer)
        account.transcript({"kind": "answer", "text": answer})
        conv = context.record_turn(account, text, answer)
        await self._fold(account, conv)

    async def _on_it(self, account: Account) -> None:
        await asyncio.sleep(self.config.on_it_after)
        await self.send(account, messages.ON_IT)

    async def solve(self, account: Account, text: str) -> str:
        task: list[dict] = [{"role": "user", "content": text}]
        ctx = ToolContext(account, self.approvals, self.config, TaskState())
        tools = self.registry.specs()
        last = ""
        for _ in range(self.config.max_model_calls):
            msgs = context.build(
                account, task, self.config.timezone, self.config.household_name, self.config.context_tokens
            )
            reply = await self.llm.chat(msgs, tools)
            log.info("model call: %d prompt tokens, %d cached", reply.prompt_tokens, reply.cached_tokens)
            if not reply.tool_calls:
                return reply.content or "…"
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
        return f"{messages.TOO_MANY_STEPS}\n{last}".strip()

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


def _short(arguments: str) -> str:
    try:
        return json.dumps(json.loads(arguments), ensure_ascii=False)[:2000]
    except json.JSONDecodeError:
        return arguments[:2000]
