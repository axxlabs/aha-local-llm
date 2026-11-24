# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The agent loop (SPEC §3.3): one continuous chat per account, tasks, approvals, commands."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol
from zoneinfo import ZoneInfo

from . import checks, context, folding, messages
from .accounts import Account, Accounts
from .approvals import Approvals
from .config import Config
from .items import Image
from .llm import LLM, ModelUnavailable
from .policy import addresses
from .tools import logins, memory, schedule, signins
from .tools.registry import Registry, TaskState, ToolContext

log = logging.getLogger("aha.agent")


MAX_RECORDED = 20_000  # characters of a model call's text or thinking kept in the transcript


def model_record(model: str, reply, secrets_typed: list[str]) -> dict:
    """One model call, for the message's details (SPEC §3.8): what it wrote, its thinking, the tools it called."""
    hide = lambda t: signins.redact(t, secrets_typed)[:MAX_RECORDED]  # noqa: E731
    return {
        "kind": "model",
        "model": model,
        "text": hide(reply.content),
        "reasoning": hide(reply.reasoning),
        "calls": [{"name": c.name, "args": hide(c.arguments)} for c in reply.tool_calls],
        "prompt_tokens": reply.prompt_tokens,
        "cached_tokens": reply.cached_tokens,
        "ms": reply.ms,
        **({"timings": reply.timings} if reply.timings else {}),
    }


class AgentOut(Protocol):
    """Where the agent's output goes: the end-to-end channel to the person's devices (e2e/channel.py)."""

    async def message(self, account: Account, text: str, image: Image | None = None, open: str = "") -> None: ...
    async def title(self, account: Account, start: int, text: str, new: bool = False) -> None: ...
    async def typing(self, account: Account, on: bool, queued: bool = False) -> None: ...
    async def card(self, account: Account, card: dict) -> None: ...
    async def state(self, account: Account, approval_id: str, state: str) -> None: ...
    async def text(self, account: Account, text: str) -> None: ...


@dataclass
class Session:
    """One person's live state on this computer (SPEC §3.3): the task running for them and what waits for it.

    Kept together so it's reset together (seen: Stop dropped the waiting messages but not a waiting photo, which
    then went with the next, unrelated request)."""

    task: asyncio.Task | None = None
    inbox: list[str] = field(default_factory=list)  # their messages that came in while the task ran
    photos: list[Image] = field(default_factory=list)  # their photos, for the next model call
    sent: int = 0  # messages sent to them, to know whether they've heard back in a task
    last_prompt: list[dict] | None = None  # the task's latest model request: its title is asked at the end

    def reset(self) -> None:
        """Drop what's waiting (Stop, Clear history, the account deleted)."""
        self.inbox.clear()
        self.photos.clear()
        self.last_prompt = None


LIMIT_TICK = 0.5  # seconds between looks at the task's time limit
TYPING_EVERY = 4.0  # seconds between `typing` frames while a reply is owed (SPEC §3.3)
STOP_WORDS = {"stop", "cancel", "stop it", "stop that"}
NEW_MESSAGE = "[New message from the user while you were working]\n{text}"
PHOTO_ONLY = "(sent a photo)"
SCHEDULED = (
    "[Scheduled task the user set up earlier{late}; they aren't necessarily looking at the chat]\n{text}\n\n"
    "{last}Do it now. If it says to tell them only when something happens and it hasn't, answer "
    "NOTHING_TO_REPORT: followed by one line on what you found. That line is kept for the next run "
    "automatically (and not sent), so don't save it as a memory."
)
LAST_TIME = "Last time you found: {note}\n\n"


class _Counted:
    """The approvals' output, counted as "the person heard from this task" (so no stray "…" follows)."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    async def card(self, account: Account, card: dict) -> None:
        self.agent.last_activity = time.monotonic()
        self.agent.session(account).sent += 1
        await self.agent.out.card(account, card)
        await self.agent._typing_frame(account, False)  # waiting for the person now, not working (SPEC §3.3)

    async def state(self, account: Account, approval_id: str, state: str) -> None:
        await self.agent.out.state(account, approval_id, state)
        if self.agent.is_running(account):  # answered: working again, said now rather than at the next tick
            await self.agent._typing_frame(account, True, self.agent.queued(account))

    async def text(self, account: Account, text: str) -> None:
        self.agent.session(account).sent += 1
        await self.agent.out.text(account, text)


class _Pausing:
    """The approvals, for a task: while a card waits for the person, the model is free for others (SPEC §3.3)."""

    def __init__(self, approvals: Approvals, llm, who: str) -> None:
        self._approvals, self._llm, self._who = approvals, llm, who

    async def ask(self, *args, **kw):
        paused = self._llm.paused(self._who) if hasattr(self._llm, "paused") else contextlib.nullcontext()
        async with paused:
            return await self._approvals.ask(*args, **kw)

    def __getattr__(self, name):
        return getattr(self._approvals, name)


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
        self.sessions: dict[str, Session] = defaultdict(Session)  # each person's live state, by address
        self.last_activity = time.monotonic()  # a message in or out, a card: `aha install --when-idle` waits on it
        self.typing_every = TYPING_EVERY

    def _turn(self, account: Account):
        """The model for this person's whole task (llm.turn), or nothing to wait for (a test's model)."""
        return self.llm.turn(account.address) if hasattr(self.llm, "turn") else contextlib.nullcontext()

    def session(self, account: Account) -> Session:
        return self.sessions[account.address]

    @property
    def running(self) -> dict[str, asyncio.Task]:
        """The tasks running now, by account address."""
        return {a: s.task for a, s in self.sessions.items() if s.task is not None}

    # ---- outbound

    async def send(self, account: Account, text: str, image: Image | None = None, open: str = "") -> None:
        self.last_activity = time.monotonic()
        self.session(account).sent += 1
        account.transcript({"kind": "out", "text": text, "image": bool(image)})
        await self.out.message(account, text[:20_000], image, open)

    # ---- inbound

    async def inbound(self, account: Account, text: str, image: Image | None = None) -> None:
        """A message the person typed, maybe with a photo (already shown on their devices by the channel)."""
        text = text.strip()
        self.last_activity = time.monotonic()
        if hasattr(self.llm, "wrote"):
            self.llm.wrote(account.address)  # their cache slot is the newest to keep (SPEC §3.2)
        account.transcript({"kind": "user", "text": text, "image": bool(image)})
        if self.browser is not None:
            self.browser.touch(account.id)  # the window stays open idle_minutes after their last message
        if image is not None:
            # A photo is never a command: it goes to the running task, or starts one (SPEC §6.5).
            self.session(account).photos.append(image)
            text = text or PHOTO_ONLY
            if await self.approvals.reply_text(account, text):
                return  # declines the open card; the running task sees the photo with the note
            await self._start(account, text)
            return
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
        if command in ("sign-ins", "signins", "sign ins"):
            kept = await self.browser_sites(account)
            lines = "\n".join(f"- {s['site']}{messages.SIGNED_IN if s['signed_in'] else ''}" for s in kept)
            await self.send(account, messages.SIGNINS.format(sites=lines) if kept else messages.NO_SIGNINS)
            return
        if command.startswith(("forget sign-in ", "forget signin ", "forget sign in ")):
            site = command.split(" ", 2)[2].removeprefix("in ").strip()
            found = await self.forget_site(account, site)
            reply = messages.SIGNIN_FORGOTTEN if found else messages.SIGNIN_UNKNOWN
            await self.send(account, reply.format(site=signins.site_name(site) or site))
            return
        if command in ("clear browser", "clear my browser", "clear browser data"):
            await self.clear_browser(account)
            await self.send(account, messages.BROWSER_CLEARED)
            return
        if command in ("reminders", "schedule", "scheduled", "scheduled tasks"):
            tz = schedule.tz_of(self.config.timezone)
            entries = sorted(schedule.load(account), key=lambda e: e["next"])
            await self.send(
                account,
                messages.REMINDERS.format(items="\n".join(f"- {schedule.describe(e, tz)}" for e in entries))
                if entries
                else messages.NO_REMINDERS,
            )
            return
        if command.startswith(("cancel reminder ", "cancel task ")):
            entry_id = command.split(" ", 2)[2].strip().strip("`")
            found = any(e["id"] == entry_id for e in schedule.load(account))
            schedule.update(account, entry_id, lambda e: None)
            await self.send(account, messages.REMINDER_CANCELLED if found else messages.REMINDER_UNKNOWN)
            return
        if command == "forget last":
            context.forget_last(account)
            await self.send(account, messages.FORGOT_LAST)
            return
        if not text:
            return
        await self._start(account, text)

    async def _start(self, account: Account, text: str) -> None:
        live = self.session(account)
        if live.task is not None:
            live.inbox.append(text)  # the running task reads it; "typing…" already shows
            return
        live.task = asyncio.create_task(self.run(account, text))

    async def approval_reply(
        self, account: Account, approval_id: str, decision: str, values: list | None = None, note: str = ""
    ) -> None:
        account.transcript({"kind": "approval_reply", "id": approval_id, "decision": decision})
        await self.approvals.answer(account, approval_id, decision, values, note)

    async def stop(self, account: Account, quiet: bool = False) -> None:
        """Cancel the running task. `quiet`: say nothing, and wait for it to wind down (Clear history, §3.8.1)."""
        live = self.session(account)
        task = live.task
        live.reset()  # what was waiting for the task goes too: nothing of it reaches a later one
        if task is None:
            if not quiet:
                await self.send(account, messages.NOTHING_TO_STOP)
            return
        task.cancel()
        if quiet:
            await asyncio.wait({task}, timeout=10)
            return
        await self.send(account, messages.STOPPED)

    # ---- tasks

    def is_running(self, account: Account) -> bool:
        return self.session(account).task is not None

    def owes_reply(self, account: Account) -> bool:
        """What "typing…" shows (SPEC §3.3): a task is running and isn't waiting for the person's approval."""
        return self.is_running(account) and self.approvals.pending_for(account.address) is None

    async def warm(self) -> None:
        """Load the chosen model and read the fixed start of every request (the system prompt and the tool
        definitions) once, so the first message after a start or a model change doesn't wait for it (§3.2)."""
        msgs = [{"role": "system", "content": context.system_prompt()}, {"role": "user", "content": "."}]
        try:
            await self.llm.chat(msgs, self.registry.specs(), max_tokens=1)
            log.info("model %s ready", self.llm.model)
        except Exception as e:
            log.warning("warming the model failed: %s", e)

    def start_scheduled(self, account: Account, entry: dict, late: bool = False) -> None:
        """A scheduled task is due (SPEC §3.4): run it as this account, like a message from them."""
        account.transcript({"kind": "scheduled", "text": entry["text"]})
        text = SCHEDULED.format(
            text=entry["text"],
            late=", running late: the computer was off" if late else "",
            last=LAST_TIME.format(note=entry["last"]) if entry.get("last") else "",
        )
        self.session(account).task = asyncio.create_task(self.run(account, text, scheduled=entry["id"]))

    async def run(self, account: Account, text: str, scheduled: str | None = None) -> None:
        held = self.browser.hold(account.id) if self.browser else contextlib.nullcontext()
        try:
            with held:
                await self._run(account, text, scheduled)
        finally:
            self.session(account).task = None
            await self.note_page(account)

    # ---- the person's browser (SPEC §3.4 "Browser profiles": it keeps everything in its own profile)

    def _browser(self, account: Account):
        """The person's own browser, or None (no browser on this computer)."""
        return self.browser.for_account(account.id) if self.browser is not None else None

    async def note_page(self, account: Account) -> None:
        """After a task: note the page that's open, where a new, blank window picks up."""
        session = self.browser.open_session(account.id) if self.browser is not None else None
        if session is None:
            return
        try:
            await signins.remember_page(account, session)
        except Exception:
            log.warning("couldn't note the browser's page")

    async def browser_sites(self, account: Account) -> list[dict]:
        """Settings → Your browser: the sites the person's browser keeps data for, read from it."""
        try:
            return await signins.sites(account, self._browser(account))
        except Exception:
            log.warning("couldn't read the browser's sites")
            return await signins.sites(account, None)

    async def forget_site(self, account: Account, site: str) -> bool:
        """Forget one site in the person's browser (cookies and storage), and its saved logins."""
        found = await signins.forget(account, self._browser(account), site)
        found = logins.forget(account, signins.site_name(site) or site) or found  # its saved logins go too (§5.8)
        return found

    async def clear_browser(self, account: Account) -> None:
        """Clear browser: stop the running task, then the person's browser, and delete their whole profile."""
        await self.stop(account, quiet=True)
        if self.browser is not None:
            await self.browser.forget(account.id)
        signins.clear(account)
        logins.clear(account)
        log.info("browser cleared for an account: its profile deleted")

    async def _run(self, account: Account, text: str, scheduled: str | None = None) -> None:
        while True:
            await self._task(account, text, scheduled)
            scheduled = None  # a follow-up is the person's own message
            # Messages that came in after the task's last model call: one follow-up task for all of them.
            pending, self.session(account).inbox = self.session(account).inbox, []
            if not pending:
                break
            text = "\n\n".join(pending)

    async def _typing(self, account: Account) -> None:
        """While the task runs: whether Aha! is working, said every few seconds. The computer is the only source
        for the app (SPEC §3.3): working, unless a card waits for the person."""
        while True:
            await self._typing_frame(account, self.owes_reply(account), self.queued(account))
            await asyncio.sleep(self.typing_every)

    async def _stop_typing(self, account: Account, typing: asyncio.Task) -> None:
        typing.cancel()
        await self._typing_frame(account, False)

    def queued(self, account: Account) -> bool:
        """Its task waits for the model while another person's runs (SPEC §3.3): the app says so."""
        return self.llm.queued(account.address)

    async def _typing_frame(self, account: Account, on: bool, queued: bool = False) -> None:
        try:
            await self.out.typing(account, on, queued)
        except Exception:
            log.warning("couldn't send a typing message")

    async def _task(self, account: Account, text: str, scheduled: str | None = None) -> None:
        # The task's records in the transcript carry its id, so a message's details can be found (SPEC §3.8).
        account.task = secrets.token_hex(6)
        try:
            await self._task_body(account, text, scheduled)
        finally:
            account.task = None

    async def _within_limit(self, account: Account, work):
        """Run the task with its time limit (SPEC §3.3), counting only the time it works: while a card waits for
        the person, the clock stops (seen: the 15 minutes ran out while the Evri sign-in card was being read)."""
        if not self.config.max_task_seconds:  # the default: no time limit, only the step limit (SPEC §3.3)
            return await work
        solving = asyncio.ensure_future(work)
        used, last = 0.0, time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({solving}, timeout=LIMIT_TICK)
                now = time.monotonic()
                if self.approvals.pending_for(account.address) is None:
                    used += now - last
                last = now
                if done:
                    return solving.result()
                if used >= self.config.max_task_seconds:
                    raise TimeoutError
        finally:
            if not solving.done():
                solving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await solving

    async def _task_body(self, account: Account, text: str, scheduled: str | None = None) -> None:
        sent_before = self.session(account).sent
        asked_at = 0 if scheduled else account.seq  # the item of the message that started it (SPEC §7.4)
        typing = asyncio.create_task(self._typing(account))
        # The model is this person's for the whole task, its title and summary included: its cache stays theirs
        # (SPEC §3.3 "One task at a time"). Waiting for the turn shows as "finishing someone else's request".
        turn = self._turn(account)
        await turn.__aenter__()
        try:
            try:
                answer, checked, text = await self._within_limit(account, self.solve(account, text))
            except asyncio.CancelledError:
                account.transcript({"kind": "stopped"})
                await self._stop_typing(account, typing)
                raise
            except TimeoutError:
                answer, checked, stopped = messages.TOO_LONG, False, True
            except ApprovalExpired:
                answer, checked, stopped = messages.APPROVAL_EXPIRED, False, True
            except ModelUnavailable as e:
                log.error("model unavailable: %s", e)
                answer, checked, stopped = messages.MODEL_DOWN, False, True
            except Exception:
                log.exception("task failed")
                answer, checked, stopped = messages.FAILED, False, True
            else:
                stopped = answer.startswith(messages.TOO_MANY_STEPS)
            if not answer.strip():
                # After "✅ Save" there may be nothing to add; if they've heard nothing at all, say something.
                answer = "" if self.session(account).sent > sent_before else "…"
            await self._stop_typing(account, typing)  # before the answer, so no refresh can land after it
            if scheduled:
                quiet = answer.strip().lstrip("*_` ").startswith(schedule.NOTHING)
                note = answer.split(schedule.NOTHING, 1)[1].lstrip(" :*_`-") if quiet else answer
                schedule.update(account, scheduled, lambda e: {**e, "last": " ".join(note.split())[:500]})
                if quiet:
                    account.transcript({"kind": "answer", "text": "", "scheduled": "nothing to report", "note": note})
                    return  # watching for a change, and there's none: say nothing, keep the chat clean
                if answer:
                    answer = f"⏰ {answer}"
            if answer:
                # Unfinished stays unfolded after a break (SPEC §7.4 "Folding"): a task that stopped, or a question.
                await self.send(account, answer, open="stopped" if stopped else "asks" if folding.asks(answer) else "")
            account.transcript({"kind": "answer", "text": answer})
            conv = context.record_turn(account, text, answer, checked)
            # The task's last request, still cached in the person's slot: the title and the summary are asked at its
            # end, so they cost only their own words (SPEC §3.2 "Cache slots", §4.4, §7.4).
            prompt, self.session(account).last_prompt = self.session(account).last_prompt, None
            if answer and not stopped:
                await self._title(account, answer, prompt, asked_at, text)
            await self._fold(account, conv, prompt, answer)
        finally:
            await turn.__aexit__(None, None, None)

    async def _title(
        self, account: Account, answer: str, prompt: list[dict] | None, asked_at: int = 0, asked: str = ""
    ) -> None:
        """Name the conversation since the last break, for when the app folds it (SPEC §7.4 "Folding").

        Asked at the end of the task's own last request, so the model server's cached prompt still matches and
        it costs only the new words; the question and the title are never part of the conversation."""
        asked = asked.split("\n\n", 1)[0]  # the message that started the task, not later notes
        start = folding.segment_start(account.recent_items(), ZoneInfo(self.config.timezone))
        if prompt is None or start is None or not hasattr(self.out, "title"):
            return
        tz = ZoneInfo(self.config.timezone)
        since = dt.datetime.fromtimestamp(start[1], tz).strftime("%H:%M")
        request = [
            *prompt,
            {"role": "assistant", "content": answer},
            {"role": "user", "content": folding.ask(since, asked)},
        ]
        try:
            reply = await self.llm.chat(
                request, self.registry.specs(), max_tokens=60, who=account.address, tool_choice="none", thinking=False
            )
        except ModelUnavailable:
            return
        new, title = folding.parse(reply.content or "")
        new = new or (bool(asked_at) and folding.says_new(asked))
        if new and asked_at > start[0]:  # a new subject: a new part starts at the person's message (SPEC §7.4)
            await self.out.title(account, asked_at, title or "", new=True)
        elif title:
            await self.out.title(account, start[0], title)

    async def solve(self, account: Account, text: str) -> tuple[str, bool, str]:
        """Returns the answer, whether it passed the checks, and everything the person said during the task."""
        task: list[dict] = [{"role": "user", "content": text}]
        has_photo = False

        def take_photos(message: dict) -> None:
            """Attach the person's waiting photos to a message for the model (the vision model reads them)."""
            nonlocal has_photo, text
            photos, self.session(account).photos = self.session(account).photos, []
            if not photos:
                return
            has_photo = True
            text = text + " [photo]" * len(photos)
            parts = [{"type": "text", "text": message["content"]}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:{i.mime};base64,{i.b64}"}} for i in photos]
            message["content"] = parts

        take_photos(task[0])

        async def send(message: str, image: Image | None = None) -> None:
            await self.send(account, checks.chat_format(message) if message else "", image)

        # The browser is reached only through this account's own context (SPEC §3.4).
        browser = self.browser.for_account(account.id) if self.browser else None
        approvals = _Pausing(self.approvals, self.llm, account.address)  # a card frees the model meanwhile
        ctx = ToolContext(account, approvals, self.config, TaskState(), browser, send)
        addresses.remember(ctx.task.seen_urls, text)  # a link the person sent is theirs, not the model's
        tools = self.registry.specs()
        last = ""
        retried: set[str] = set()  # the checks that already sent a draft back (SPEC §3.5.1): once each
        can_sign_in = "browser_login" in self.registry.names()
        # Where numbers in the answer may come from: what the person said or saved, earlier answers, and pages
        # opened in this task. Search snippets don't count (SPEC §3.5).
        conv = account.load_context()
        sources = [
            text,
            *(m["text"] for m in account.load_memory()),
            *(t["content"] for t in conv.get("turns", []) if t["role"] == "user" or t.get("checked")),
        ]
        for _ in range(self.config.max_model_calls):
            extras, self.session(account).inbox = self.session(account).inbox, []
            for extra in extras:
                task.append({"role": "user", "content": NEW_MESSAGE.format(text=extra)})
                sources.append(extra)
                addresses.remember(ctx.task.seen_urls, extra)
                text = f"{text}\n\n{extra}"
                take_photos(task[-1])
            msgs = context.build(
                account, task, self.config.timezone, self.config.household_name, self.config.context_tokens
            )
            reply = await self.llm.chat(msgs, tools, who=account.address)
            self.session(account).last_prompt = msgs  # the title is asked at its end (SPEC §7.4 "Folding")
            log.info("model call: %d prompt tokens, %d cached", reply.prompt_tokens, reply.cached_tokens)
            account.transcript(model_record(self.llm.model, reply, ctx.task.secrets))
            if not reply.tool_calls:
                draft = checks.Draft(text, reply.content or "", ctx.task, can_sign_in)
                failed = next((c for c in checks.CLAIM_CHECKS if c.name not in retried and c.failed(draft)), None)
                if failed:
                    retried.add(failed.name)
                    log.info(failed.log)
                    account.transcript({"kind": "check", "name": failed.name})
                    task.append({"role": "assistant", "content": draft.answer})
                    retry = failed.retry_for(draft)
                    task.append({"role": "user", "content": retry})
                    continue
                if has_photo:
                    # Numbers in the person's photo can't be matched as text: no retry, no warning, and the
                    # answer isn't a source later (SPEC §3.5.1).
                    return checks.chat_format(reply.content or ""), False, text
                missing = checks.unverified(checks.chat_format(reply.content or ""), sources)
                if missing and "numbers" not in retried:
                    # One more go: ask the model to confirm the numbers on a page, or drop them (SPEC §3.5).
                    retried.add("numbers")
                    account.transcript({"kind": "check", "name": "numbers", "missing": missing[:20]})
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
                addresses.remember(ctx.task.seen_urls, result.text)  # links the task was shown (SPEC §5.2)
                if result.kind == "expired":
                    raise ApprovalExpired
                if ctx.task.secrets:  # what the person typed on a sign-in card never shows again (SPEC §3.4)
                    result.text = signins.redact(result.text, ctx.task.secrets)
                account.transcript(
                    {
                        "kind": "tool",
                        "name": call.name,
                        "args": signins.redact(_short(call.arguments), ctx.task.secrets),
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
        return (*finish(f"{messages.TOO_MANY_STEPS}\n{last}".strip(), sources), text)

    async def _fold(self, account: Account, conv: dict, prompt: list[dict] | None = None, answer: str = "") -> None:
        """Between tasks only: fold the oldest turns into the summary (SPEC §4.4). Asked at the end of the task's
        own cached prompt, in the person's slot, where those turns are already read; a prompt of its own only when
        there's none (a quiet scheduled task)."""
        folded = context.turns_to_fold(conv)
        if not folded:
            return
        if prompt is not None:
            request = [
                *prompt,
                {"role": "assistant", "content": answer},
                {"role": "user", "content": context.fold_ask(folded)},
            ]
            tools, choice = self.registry.specs(), "none"  # the same tools, so the cached prompt still matches
        else:
            request, tools, choice = context.fold_request(conv, folded), None, "auto"
        try:
            reply = await self.llm.chat(
                request, tools, max_tokens=1500, who=account.address, tool_choice=choice, thinking=False
            )
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
