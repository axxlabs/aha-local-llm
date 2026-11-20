"""The agent loop (SPEC §3.3): one continuous chat per account, tasks, approvals, commands."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from . import checks, context, messages
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
    def __init__(
        self, config: Config, accounts: Accounts, llm: LLM, registry: Registry, send_out: SendOut, browser=None
    ) -> None:
        self.config = config
        self.accounts = accounts
        self.llm = llm
        self.registry = registry
        self.send_out = send_out
        self.browser = browser
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
            answer, checked = await asyncio.wait_for(self.solve(account, text), timeout=self.config.max_task_seconds)
        except asyncio.CancelledError:
            account.transcript({"kind": "stopped"})
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
        finally:
            on_it.cancel()
        await self.send(account, answer)
        account.transcript({"kind": "answer", "text": answer})
        conv = context.record_turn(account, text, answer, checked)
        await self._fold(account, conv)

    async def _on_it(self, account: Account) -> None:
        """Once, after a while, unless the person has already heard from this task (an approval)."""
        asked_before = self.approvals.asked.get(account.address, 0)
        await asyncio.sleep(self.config.on_it_after)
        if self.approvals.asked.get(account.address, 0) == asked_before:
            await self.send(account, messages.ON_IT)

    async def solve(self, account: Account, text: str) -> tuple[str, bool]:
        task: list[dict] = [{"role": "user", "content": text}]
        ctx = ToolContext(account, self.approvals, self.config, TaskState(), self.browser)
        tools = self.registry.specs()
        last = ""
        retried = False
        # Where numbers in the answer may come from: what the person said or saved, earlier answers, and pages
        # opened in this task. Search snippets don't count (SPEC §3.5).
        conv = account.load_context()
        sources = [
            text,
            *(m["text"] for m in account.load_memory()),
            *(t["content"] for t in conv.get("turns", []) if t["role"] == "user" or t.get("checked")),
        ]
        for _ in range(self.config.max_model_calls):
            msgs = context.build(
                account, task, self.config.timezone, self.config.household_name, self.config.context_tokens
            )
            reply = await self.llm.chat(msgs, tools)
            log.info("model call: %d prompt tokens, %d cached", reply.prompt_tokens, reply.cached_tokens)
            if not reply.tool_calls:
                missing = checks.unverified(checks.chat_format(reply.content or ""), sources)
                if missing and not retried:
                    # One more go: ask the model to confirm the numbers on a page, or drop them (SPEC §3.5).
                    retried = True
                    log.info("draft answer had %d unconfirmed number(s); asking the model to check", len(missing))
                    task.append({"role": "assistant", "content": reply.content or ""})
                    task.append({"role": "user", "content": checks.retry_prompt(missing)})
                    continue
                return finish(reply.content or "…", sources)
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
        return finish(f"{messages.TOO_MANY_STEPS}\n{last}".strip(), sources)

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
    """WhatsApp formatting, and a visible warning for numbers no opened page supports.

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
