# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The agent loop (SPEC §3.3): one continuous chat per account, tasks, approvals, commands."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from . import actions, checks, context, folding, messages, skills, topics
from .accounts import TASK, Account, Accounts
from .approvals import Approvals
from .config import Config
from .items import Image
from .llm import Model, ModelUnavailable
from .policy import addresses
from .tools import background, signins
from .tools.registry import Registry, TaskState, ToolContext, redacted

log = logging.getLogger("aha.agent")


MAX_RECORDED = 20_000  # characters of a model call's text or thinking kept in the transcript


def model_record(model: str, reply) -> dict:
    """One model call, for the message's details (SPEC §3.8): what it wrote, its thinking, the tools it called."""
    hide = lambda t: (t or "")[:MAX_RECORDED]  # noqa: E731
    return {
        "kind": "model",
        "model": model,
        "text": hide(reply.content),
        "reasoning": hide(reply.reasoning),
        # Each call's label in the person's words (SPEC §7.4 "Details"): what the app's details line says it's doing
        "calls": [
            {"name": c.name, "args": hide(c.arguments), "label": messages.tool_label(c.name) or ""}
            for c in reply.tool_calls
        ],
        "prompt_tokens": reply.prompt_tokens,
        "cached_tokens": reply.cached_tokens,
        "ms": reply.ms,
        **({"timings": reply.timings} if reply.timings else {}),
    }


class AgentOut(Protocol):
    """Where the agent's output goes: the end-to-end channel to the person's devices (e2e/channel.py)."""

    async def message(
        self,
        account: Account,
        text: str,
        image: Image | None = None,
        run: str = "",
    ) -> None: ...
    async def steps(self, account: Account) -> None: ...  # the request's Details line (SPEC §7.4 "Details")
    async def act(self, account: Account, text: str) -> None: ...  # one thing a request did, as it happens
    async def step(self, account: Account, event: dict) -> None: ...  # one of its records, as it happens
    async def typing(self, account: Account, on: bool, queued: bool = False, wake: bool = False) -> None: ...
    async def card(self, account: Account, card: dict) -> None: ...
    async def state(self, account: Account, approval_id: str, state: str) -> None: ...
    async def text(self, account: Account, text: str) -> None: ...


@dataclass(frozen=True)
class State:
    """What runs for one person (SPEC §3.3): nothing, their request, or a topic's wakeup ("check"); and whether it
    waits on a card for them (then the model is free for others)."""

    kind: str  # idle | request | check
    card: bool = False  # waiting on a card

    @property
    def model_busy(self) -> bool:
        return self.kind != "idle" and not self.card

    @property
    def owes_reply(self) -> bool:
        return self.kind == "request" and not self.card


@dataclass
class Session:
    """One person's live state on this computer (SPEC §3.3): the task running for them and what waits for it.

    Kept together so it's reset together (seen: Stop dropped the waiting messages but not a waiting photo, which
    then went with the next, unrelated request)."""

    task: asyncio.Future | None = None  # the request or wakeup running for them
    topic: int = 0  # the topic it works in (SPEC §4.1)
    wake: bool = False  # it's a wakeup: the topic woke up on its own
    inbox: list[str] = field(default_factory=list)  # their messages to that topic that came in while it ran
    queue: list[tuple[int, str]] = field(default_factory=list)  # their messages to other topics: each runs next
    photos: list[Image] = field(default_factory=list)  # their photos, for the next model call
    sent: int = 0  # messages sent to them, to know whether they've heard back in a task
    last_prompt: list[dict] | None = None  # the task's latest model request: its title is asked at the end

    def reset(self) -> None:
        """Drop what's waiting (Stop, Clear history, the account deleted)."""
        self.inbox.clear()
        self.queue.clear()
        self.photos.clear()
        self.last_prompt = None


TYPING_EVERY = 4.0  # seconds between `typing` frames while a reply is owed (SPEC §3.3)
STOP_WORDS = {"stop", "cancel", "stop it", "stop that"}
NEW_MESSAGE = "[New message from the user while you were working]\n{text}"
UNSENT = "[sha: your reply above wasn't sent: the user wrote again. Reply to everything in one answer.]"
PHOTO_ONLY = "(sent a photo)"


WOKE = "[This topic woke up on its own, check {n}]"  # a wakeup's turn, as later runs in the topic see it


@dataclass(frozen=True)
class Job:
    """A run the person didn't start (SPEC §4.1): a topic's wakeup. It shows no "working…", only what it does (its
    ✓ lines and cards, in the topic); its schedule decides what of its answer reaches the chat."""

    topic: int

    def finish(self, account: Account, outcome: str, message: str, stopped: bool) -> str:
        """What to show of its answer: "" for nothing, else its outcome (background.finish)."""
        return background.finish(account, self.topic, outcome, message, stopped)

    def links(self, account: Account) -> list[str]:
        """The addresses its instructions may open: what the request that deferred it was shown, or the person
        wrote in it (SPEC §5.2 "Made-up addresses")."""
        return background.links_of(account, self.topic)

    def interrupted(self, account: Account) -> None:
        """Stopped before it finished (someone's input, Stop): the next wakeup is told."""
        background.interrupted(account, self.topic)


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
        # Answered: working again, said now rather than at the next tick. Not for a background check, which
        # shows nothing while it works and never turns it off (seen: "sha is working…" stayed after a check's
        # note, once a card in it was answered).
        if self.agent.state(account.address).kind == "request":
            await self.agent._typing_frame(account, True, self.agent.queued(account))

    async def text(self, account: Account, text: str) -> None:
        self.agent.session(account).sent += 1
        await self.agent.out.text(account, text)


class _Pausing:
    """The approvals, for a task: while a card waits for the person, the model is free for others (SPEC §3.3)."""

    def __init__(self, approvals: Approvals, llm, who: str) -> None:
        self._approvals, self._llm, self._who = approvals, llm, who

    async def ask(self, *args, **kw):
        async with self._llm.paused(self._who):
            return await self._approvals.ask(*args, **kw)

    def __getattr__(self, name):
        return getattr(self._approvals, name)


class Agent:
    def __init__(
        self, config: Config, accounts: Accounts, llm: Model, registry: Registry, out: AgentOut, browser=None
    ) -> None:
        self.config = config
        self.accounts = accounts
        self.llm = llm
        self.registry = registry
        self.out = out
        self.browser = browser
        self.approvals = Approvals(_Counted(self))
        self.sessions: dict[str, Session] = defaultdict(Session)  # each person's live state, by address
        self.last_activity = time.monotonic()  # a message in or out, a card: `aha install --when-idle` waits on it
        self.last_input = time.time()  # the household's last message or card answer: background checks wait on it
        self.typing_every = TYPING_EVERY
        self.browser_actions = actions.Browser(self)  # Settings → Your browser, and the chat commands for it

    def _turn(self, account: Account):
        """The model for this person's whole task (llm.turn)."""
        return self.llm.turn(account.address)

    def session(self, account: Account) -> Session:
        return self.sessions[account.address]

    def state(self, address: str) -> State:
        """One person's state, the one source for every "is it busy?" question (SPEC §3.3)."""
        live = self.sessions.get(address)
        if live is None or live.task is None:
            return State("idle")
        return State("check" if live.wake else "request", self.approvals.pending_for(address) is not None)

    @property
    def running(self) -> dict[str, asyncio.Future]:
        """The people's requests running now, by account address (a background check isn't one)."""
        return {a: s.task for a, s in self.sessions.items() if s.task is not None and self.state(a).kind == "request"}

    def busy(self) -> bool:
        """The model is at work for someone (a request or a check), not waiting on a card: one waiting for the
        person, maybe for days, leaves the model free."""
        return any(self.state(a).model_busy for a in self.sessions)

    def working(self) -> bool:
        """A person's request is being worked on (the idle line says so)."""
        return any(self.state(a).owes_reply for a in self.sessions)

    def idle(self, now: float | None = None) -> bool:
        """Nobody typed or answered a card for background.IDLE_AFTER seconds (SPEC §3.4 "Background tasks")."""
        return (time.time() if now is None else now) - self.last_input >= background.IDLE_AFTER

    def _input(self) -> None:
        """The person's input: it resets the idle time (a background check never does)."""
        self.last_activity = time.monotonic()
        self.last_input = time.time()

    async def _yield_to_people(self, answering: str = "") -> None:
        """Someone wrote or answered a card: a background check that's working (not waiting on a card, nor the
        one being answered) stops, so the model is theirs. It runs again at the next idle time."""
        for address, live in list(self.sessions.items()):
            task = live.task
            if not live.wake or task is None or address == answering:
                continue
            if self.approvals.pending_for(address) is not None:
                continue
            task.cancel()
            await asyncio.wait({task}, timeout=10)

    # ---- outbound

    async def send(
        self,
        account: Account,
        text: str,
        image: Image | None = None,
        run: str = "",
    ) -> None:
        """`run`: the request it ends (its Details line, SPEC §7.4)."""
        self.last_activity = time.monotonic()
        self.session(account).sent += 1
        account.transcript({"kind": "out", "text": text, "image": bool(image)})
        await self.out.message(account, text[:20_000], image, run)

    # ---- inbound

    async def inbound(self, account: Account, text: str, image: Image | None = None, topic: int = 0) -> None:
        """A message the person typed in `topic`, maybe with a photo (already shown on their devices by the
        channel, which chose the topic: the one open in their app, or a new one, SPEC §4.1 "Routing")."""
        topic = topic or topics.new(account, first=account.seq)  # every message belongs to a topic (SPEC §4.1)
        token = topics.TOPIC.set(topic)  # what's said straight back (help, stopped) goes in that topic
        try:
            await self._inbound(account, text, image, topic)
        finally:
            topics.TOPIC.reset(token)

    async def _inbound(self, account: Account, text: str, image: Image | None, topic: int) -> None:
        text = text.strip()
        self._input()
        await self._yield_to_people()
        self.llm.wrote(account.address)  # their cache slot is the newest to keep (SPEC §3.2)
        account.transcript({"kind": "user", "text": text, "image": bool(image)})
        if image is not None:
            # A photo is never a command: it goes to the running task, or starts one (SPEC §6.5).
            self.session(account).photos.append(image)
            text = text or PHOTO_ONLY
            self.approvals.cancel_for_message(account)
            await self._start(account, text, topic)
            return
        command = text.lower().rstrip(".!")

        if command in STOP_WORDS:
            await self.stop(account)
            return
        self.approvals.cancel_for_message(account)
        if command == "help":
            await self.send(account, messages.HELP.format(source=self.config.source_url))
            return
        if not text:
            return
        await self._start(account, text, topic)

    async def _start(self, account: Account, text: str, topic: int) -> None:
        if topic:
            topics.touch(account, topic)
            if not self.session(account).wake or self.session(account).topic != topic:
                topics.answered(account, topic)  # it no longer waits on them
                background.resume(account, topic)
        live = self.session(account)
        if live.task is not None:
            if live.topic == topic and not live.wake:
                live.inbox.append(text)  # the running task reads it; "typing…" already shows
            else:
                live.queue.append((topic, text))  # another topic: it runs next (SPEC §4.1 "One run per topic")
            return
        live.topic, live.wake = topic, False
        live.task = asyncio.create_task(self.run(account, text, topic))

    async def approval_reply(
        self,
        account: Account,
        approval_id: str,
        decision: str,
        values: list | None = None,
        note: str = "",
        always: bool = False,
    ) -> None:
        account.transcript({"kind": "approval_reply", "id": approval_id, "decision": decision})
        self._input()
        await self._yield_to_people(answering=account.address)
        await self.approvals.answer(account, approval_id, decision, values, note, always)

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
        """A request or a check runs for this person (waiting on a card or not)."""
        return self.state(account.address).kind != "idle"

    def runs_in(self, account: Account, topic: int) -> bool:
        """A request or wakeup runs for this person in that topic."""
        return self.is_running(account) and self.session(account).topic == topic

    def owes_reply(self, account: Account) -> bool:
        """What "typing…" shows (SPEC §3.3): their request is running and isn't waiting for their approval."""
        return self.state(account.address).owes_reply

    async def warm(self) -> None:
        """Load the chosen model and read the fixed start of every request (the system prompt and the tool
        definitions) once, so the first message after a start or a model change doesn't wait for it (§3.2)."""
        msgs = [{"role": "system", "content": context.system_prompt(self.llm.model)}, {"role": "user", "content": "."}]
        try:
            await self.llm.chat(msgs, self.registry.specs(), max_tokens=1)
            log.info("model %s ready", self.llm.model)
        except Exception as e:
            log.warning("warming the model failed: %s", e)

    async def start_wake(self, account: Account, n: int, text: str) -> None:
        """Topic n wakes up on its own, while the household is idle or on Run now (SPEC §4.1): it runs in the topic,
        with its turns in view, as this account."""
        live = self.session(account)
        if live.task is not None:
            return
        live.topic, live.wake = n, True
        account.transcript({"kind": "wake", "topic": n})
        live.task = asyncio.create_task(self.run(account, text, n, Job(n)))

    async def run(self, account: Account, text: str, topic: int = 0, job: Job | None = None) -> None:
        try:
            await self._run(account, text, topic, job)
        finally:
            live = self.session(account)
            live.task, live.topic, live.wake = None, 0, False
            await self.note_page(account)

    # ---- the person's browser (SPEC §3.4 "Browser profiles": it keeps everything in its own profile)

    async def note_page(self, account: Account) -> None:
        """After a task: note the page that's open, where a new, blank window picks up."""
        session = self.browser.open_session(account.id) if self.browser is not None else None
        if session is None:
            return
        try:
            await signins.remember_page(account, session)
        except Exception:
            log.warning("couldn't note the browser's page")

    async def _run(self, account: Account, text: str, topic: int, job: Job | None = None) -> None:
        live = self.session(account)
        while True:
            token = topics.TOPIC.set(topic or None)
            try:
                await self._request(account, text, topic, job)
            finally:
                topics.TOPIC.reset(token)
            job, live.wake = None, False  # a follow-up is the person's own message
            # Messages to this topic that came in after the task's last model call: one follow-up for all of them;
            # then each other topic the person wrote in meanwhile, in order.
            pending, live.inbox = live.inbox, []
            if pending:
                text = "\n\n".join(pending)
                continue
            if not live.queue:
                break
            topic, text = live.queue.pop(0)
            live.inbox = [t for n, t in live.queue if n == topic]
            live.queue = [(n, t) for n, t in live.queue if n != topic]
            live.topic = topic

    async def _typing(self, account: Account) -> None:
        """While the task runs: whether sha is working, said every few seconds. The computer is the only source
        for the app (SPEC §3.3): working, unless a card waits for the person."""
        while True:
            await self._typing_frame(account, self.owes_reply(account), self.queued(account))
            await asyncio.sleep(self.typing_every)

    async def _stop_typing(self, account: Account, typing: asyncio.Task, quiet: bool = False) -> None:
        """Said off at the end of every run, a wakeup's too: harmless when it was never on."""
        typing.cancel()
        await self._typing_frame(account, False, wake=quiet)

    def queued(self, account: Account) -> bool:
        """Its task waits for the model while another person's runs (SPEC §3.3): the app says so."""
        return self.llm.queued(account.address)

    async def _typing_frame(self, account: Account, on: bool, queued: bool = False, wake: bool = False) -> None:
        """`wake`: a topic's wakeup works (or stopped): the app shows it on the topic's line only, no "working…"
        and no Stop (SPEC §4.1 "Icons")."""
        try:
            await self.out.typing(account, on, queued, wake)
        except Exception:
            log.warning("couldn't send a typing message")

    async def _request(self, account: Account, text: str, topic: int, job: Job | None = None) -> None:
        # The task's records in the transcript carry its id, so a message's details can be found (SPEC §3.8).
        token = TASK.set(secrets.token_hex(6))
        try:
            await self._request_body(account, text, topic, job)
        finally:
            TASK.reset(token)

    async def _request_body(self, account: Account, text: str, topic: int, job: Job | None = None) -> None:
        sent_before = self.session(account).sent
        quiet = job is not None  # a wakeup: no "working…", no Details line; its ✓ lines and cards still show
        record: list[str] = []  # what it changed, kept with its answer (SPEC §4 "What a request did")
        report: dict = {}  # a background check's check_result: {"outcome", "message"} (SPEC §3.4)
        typing = asyncio.create_task(self._typing_frame(account, True, wake=True) if quiet else self._typing(account))
        # The model is this person's for the whole task, its title and summary included: its cache stays theirs
        # (SPEC §3.3 "One task at a time"). Waiting for the turn shows as "finishing someone else's request".
        turn = self._turn(account)
        await turn.__aenter__()
        try:
            try:
                answer, text = await self.solve(
                    account,
                    text,
                    quiet,
                    job,
                    job.links(account) if job else None,
                    record,
                    report if job else None,
                    topic,
                )
            except asyncio.CancelledError:
                account.transcript({"kind": "stopped"})
                if job:
                    job.interrupted(account)
                else:  # what it did before it stopped stays known (seen: a saved fact saved twice more)
                    context.record_turn(account, topic, text, messages.STOPPED, record=record)
                await self._stop_typing(account, typing, quiet)
                if record and not job:  # its Details line comes after "Stopped."
                    with contextlib.suppress(Exception):
                        await self.send(account, "", run=TASK.get() or "")
                raise
            except ModelUnavailable as e:
                log.error("model unavailable: %s", e)
                answer, ended = messages.MODEL_DOWN, "model_down"
            except Exception:
                log.exception("task failed")
                answer, ended = messages.FAILED, "failed"
            else:
                ended = "step_limit" if answer.startswith(messages.TOO_MANY_STEPS) else ""
            stopped = bool(ended)  # it didn't finish: the reason is `ended` (Stop raises CancelledError above)
            if not answer.strip() and not job:
                # After "✅ Save" there may be nothing to add; if they've heard nothing at all, say something
                # (its ✓ lines are something).
                answer = "" if self.session(account).sent > sent_before or record else "…"
            await self._stop_typing(account, typing, quiet)  # before the answer, so no refresh can land after it
            asked = text
            if job:
                checks_before = (background.schedule_of(account, topic) or {}).get("checks", 0)
                kind = job.finish(account, report.get("outcome", ""), report.get("message", answer), stopped)
                if not kind:
                    # Nothing to tell yet: only its ✓ lines show, and the topic's turns don't grow (SPEC §4.1).
                    account.transcript({"kind": "answer", "text": "", "job": "wake", "quiet": True})
                    return
                answer = report.get("message", answer)
                asked = WOKE.format(n=checks_before + 1)
            if answer or record:
                run = "" if quiet else TASK.get() or ""
                await self.send(account, answer, run=run)
            account.transcript({"kind": "answer", "text": answer, **({"ended": ended} if ended else {})})
            context.record_turn(account, topic, asked, answer, record)
            # The task's last request, still cached in the person's slot: the topic's title and memory entry are
            # asked at its end, so they cost only their own words (SPEC §3.2 "Cache slots", §5.4, §7.4).
            prompt, self.session(account).last_prompt = self.session(account).last_prompt, None
            if answer and topic:
                await self._topic(account, topic, answer, prompt, text, waits=not stopped and not job)
        finally:
            await turn.__aexit__(None, None, None)

    async def _topic(
        self,
        account: Account,
        topic: int,
        answer: str,
        prompt: list[dict] | None,
        asked: str = "",
        waits: bool = True,
    ) -> None:
        """Name the topic and write its memory entry, what the model will know of it once its messages leave the
        context (§5.4); `waits`: whether the model may say its answer waits for the person's reply (the topic then
        shows under Needs input, SPEC §4.1). A wakeup's waiting is its check_result's.

        Asked at the end of the task's own last request, so the model server's cached prompt still matches and
        it costs only the new words; the question and the reply are never part of the conversation."""
        if prompt is None:
            return
        asked = asked.split("\n\n", 1)[0]  # the message that started the task, not later notes
        request = after_task(prompt, answer, folding.ask(asked))
        try:
            reply = await self.llm.chat(
                request, self.registry.specs(), max_tokens=500, who=account.address, tool_choice="none", thinking=False
            )
        except ModelUnavailable:
            return
        found = folding.parse(reply.content or "")
        entry = topics.set_title(account, topic, found.title) if found.title else topics.get(account, topic)
        title = (entry or {}).get("title", "")
        context.save_topic(account, topic, title, found.memory)
        if waits and found.waiting:
            topics.wait_on_person(account, topic, answer)

    async def solve(
        self,
        account: Account,
        text: str,
        quiet: bool = False,
        job: Job | None = None,
        links: list[str] | None = None,
        record: list[str] | None = None,
        report: dict | None = None,
        topic: int = 0,
    ) -> tuple[str, str]:
        """Returns the answer and everything the person said during the task.
        `quiet` (a wakeup): no Details line, and the person's messages wait for a run of their own. `job`: the
        wakeup's topic schedule. `record`: filled with what it changes, one line each (SPEC §4 "What a request
        did"), kept by the caller even if it stops. `report`: a wakeup's, filled by its check_result call. `topic`:
        the topic it works in: its turns are in view, and what its cards were allowed "always" carries over
        (SPEC §4.1, §5.3)."""

        task: list[dict] = [{"role": "user", "content": text}]

        def take_photos(message: dict) -> None:
            """Attach the person's waiting photos to a message for the model (the vision model reads them)."""
            nonlocal text
            photos, self.session(account).photos = self.session(account).photos, []
            if not photos:
                return
            text = text + " [photo]" * len(photos)
            message["content"] = with_images(message["content"], photos)

        take_photos(task[0])

        async def send(message: str, image: Image | None = None) -> None:
            await self.send(account, message, image)

        # The browser is reached only through this account's own context (SPEC §3.4).
        browser = self.browser.for_account(account.id) if self.browser else None
        approvals = _Pausing(self.approvals, self.llm, account.address)  # a card frees the model meanwhile
        state = TaskState(
            allowed=set((topics.get(account, topic) or {}).get("allowed", [])),
            topic=topic,
            wake=job is not None,
            record=record if record is not None else [],
            report=report,
        )
        ctx = ToolContext(account, approvals, self.config, state, browser, send)

        steps_shown = False

        async def record(entry: dict) -> None:
            """The task's transcript: what the person typed on a sign-in card never shows (SPEC §5.8). Each record
            also goes to the app as it happens, under one Details line for the request (SPEC §7.4 "Details"); a
            background check is quiet."""
            nonlocal steps_shown
            entry = redacted(entry, ctx.task.secrets)
            account.transcript(entry)
            if quiet:
                return
            if not steps_shown:
                steps_shown = True
                await self.out.steps(account)
            await self.out.step(account, entry)

        # A link the person sent is theirs, not the model's. A check's text is its task's instructions, which the
        # model wrote: only the links its task kept count (SPEC §5.2 "Made-up addresses").
        addresses.remember(ctx.task.seen_urls, " ".join(links or []) if quiet else text)
        tools = self.registry.specs()
        retried: set[str] = set()  # the page-state checks that already sent a draft back (SPEC §3.5.1): once each
        asked_report = False  # a background check was asked once to call check_result (SPEC §3.4)
        can_sign_in = "browser_login" in self.registry.names()
        unsent = False  # the model's last reply wasn't sent: the person wrote again (SPEC §3.3)
        for _ in range(self.config.max_model_calls):
            extras, self.session(account).inbox = (
                ([], self.session(account).inbox)
                if quiet
                else (
                    self.session(account).inbox,
                    [],
                )
            )
            for extra in extras:
                said = NEW_MESSAGE.format(text=extra)
                task.append({"role": "user", "content": f"{UNSENT}\n\n{said}" if unsent else said})
                unsent = False
                addresses.remember(ctx.task.seen_urls, extra)
                text = f"{text}\n\n{extra}"
                take_photos(task[-1])
            msgs = context.build(
                account,
                task,
                self.config.timezone,
                self.config.household_name,
                self.config.context_tokens,
                topic=topic,
                model=self.llm.model,
            )
            reply = await self.llm.chat(msgs, tools, who=account.address)
            self.session(account).last_prompt = msgs  # the title is asked at its end (SPEC §7.4 "Folding")
            log.info("model call: %d prompt tokens, %d cached", reply.prompt_tokens, reply.cached_tokens)
            await record(model_record(self.llm.model, reply))
            if not reply.tool_calls:
                answer = reply.content or ""
                if not quiet and self.session(account).inbox:
                    # They wrote after the model's last call: one more call, so the answer covers it too
                    # (SPEC §3.3), rather than a separate request after this one.
                    task.append({"role": "assistant", "content": answer})
                    unsent = True
                    continue
                draft = checks.Draft(ctx.task, can_sign_in)
                failed = next((c for c in checks.CLAIM_CHECKS if c.name not in retried and c.failed(draft)), None)
                if failed:
                    retried.add(failed.name)
                    log.info(failed.log)
                    await record({"kind": "check", "name": failed.name})
                    task.append({"role": "assistant", "content": answer})
                    task.append({"role": "user", "content": failed.retry_for(draft)})
                    continue
                if report is not None and not report and not asked_report:
                    # A check reports what it found with check_result (SPEC §3.4): a tool call, not words to parse.
                    asked_report = True
                    await record({"kind": "check", "name": "report"})
                    task.append({"role": "assistant", "content": answer})
                    task.append({"role": "user", "content": background.REPORT_RETRY})
                    continue
                return answer, text
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
                before = len(ctx.task.record)
                result = await self.registry.call(call.name, call.arguments, ctx)
                # Links the task was shown (SPEC §5.2): the whole page's, whose view leaves the addresses out (§3.4)
                addresses.remember(ctx.task.seen_urls, result.full or result.text)
                await record(
                    {
                        "kind": "tool",
                        "name": call.name,
                        "label": messages.tool_label(call.name) or "",
                        "args": _short(call.arguments),
                        "result": result.text[:4000],
                        "error": result.error,
                        # What it changed, as the record has it (SPEC §4 "What a request did")
                        **({"did": action_lines(ctx.task.record[before:])} if len(ctx.task.record) > before else {}),
                    }
                )
                # Every call, in its topic, a wakeup's too (SPEC §4.1, §7.4 "What sha did")
                lines = action_lines(ctx.task.record[before:]) or [
                    messages.call_line(call.name, _args(call.arguments), result.error)
                ]
                for line in lines:
                    await self.out.act(account, line)
                # A site's or a tool's skill, the first time the task reaches it (SPEC §3.5 "Skills"): after the
                # result, and kept when an older page is shortened (context.shorten_task).
                due = skills.due(ctx.task, call.name)
                skill = "\n\n".join(skills.added(k) for k in due)
                if not quiet:  # shown like a skill the model read (SPEC §7.4 "What sha did")
                    for k in due:
                        await self.out.act(account, messages.skill_line(k.name))
                task.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": (result.text or "(no output)") + (f"\n\n{skill}" if skill else ""),
                        "_kind": result.kind,
                        **({"_skill": skill} if skill else {}),
                    }
                )
                for image in result.images:
                    shot = with_images(f"[Screenshot returned by {call.name}]", [image])
                    task.append({"role": "user", "_kind": "image", "content": shot})

            if report:  # the check said what it found: it's over (SPEC §3.4)
                return report.get("message", ""), text
        return out_of_steps(ctx.task.record), text


def with_images(text: str, images: list[Image]) -> list[dict]:
    """A message's content with pictures for the vision model: the person's photos, or a tool's screenshot."""
    parts = [{"type": "text", "text": text}]
    return parts + [{"type": "image_url", "image_url": {"url": f"data:{i.mime};base64,{i.b64}"}} for i in images]


def after_task(prompt: list[dict], answer: str, question: str) -> list[dict]:
    """A question asked at the end of the task's own last request (its title, a summary): the model server's
    cached prompt still matches, so it costs only the new words (SPEC §3.2 "Cache slots")."""
    return [*prompt, {"role": "assistant", "content": answer}, {"role": "user", "content": question}]


def out_of_steps(record: list[str]) -> str:
    """The answer when the step limit ends a task (SPEC §3.3), never the model's last words (seen: "Let me take a
    fresh snapshot to see the current state." sent as the answer). What it did shows above it, as its ✓ lines."""
    return messages.TOO_MANY_STEPS if record else f"{messages.TOO_MANY_STEPS} Nothing was changed."


MAX_ACTION_LINE = 200


def action_lines(record: list[str] | None) -> list[str]:
    """A request's record as the chat's ✓ lines (SPEC §7.4 "Action lines"): each action that changed something, in
    the tool's own words, without the tool's name; never written by the model."""
    out = []
    for line in record or []:
        text = line.split(": ", 1)[-1].strip()
        out.append(text if len(text) <= MAX_ACTION_LINE else text[: MAX_ACTION_LINE - 1] + "…")
    return out


def _args(arguments: str) -> dict:
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _short(arguments: str) -> str:
    try:
        return json.dumps(json.loads(arguments), ensure_ascii=False)[:2000]
    except json.JSONDecodeError:
        return arguments[:2000]
