# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The model server client: OpenAI-compatible /v1/chat/completions on llama.cpp (SPEC §3.2, §3.3)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("aha.llm")


TIMINGS = ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "cache_n")


class ModelUnavailable(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    cached_tokens: int = 0
    reasoning: str = ""  # the model's thinking, when the server returns it separately
    ms: int = 0  # how long the call took, waiting for the GPU included
    timings: dict = field(default_factory=dict)  # llama.cpp's: prompt_ms, prompt_n, predicted_ms, predicted_n


class Slots:
    """Which of the model server's cache slots each person uses (SPEC §3.2 "Cache slots").

    Every request starts with the same system prompt and tools, so the server's own choice (the slot whose cache
    matches best) put everyone in one slot, and each person's request threw out the other's conversation. Here each
    active person keeps a slot of their own; the last one is for background work (summaries), so that never
    throws a conversation out either. A new person always gets a slot: when all are taken, the person whose last
    message is oldest gives theirs up until they write again."""

    def __init__(self, count: int, last: dict[str, float]) -> None:
        self.count = count
        self.owner: dict[int, str] = {}  # slot → person
        self.last = last  # person → when they last wrote (kept by the client)

    @property
    def background(self) -> int | None:
        return self.count - 1 if self.count >= 2 else None

    def of(self, who: str) -> int | None:
        if self.count < 2:  # one slot: nothing to choose
            return None
        for slot, owner in self.owner.items():
            if owner == who:
                return slot
        mine = range(self.count - 1)
        free = [s for s in mine if s not in self.owner]
        slot = free[0] if free else min(mine, key=lambda s: self.last.get(self.owner[s], 0.0))
        if slot in self.owner:
            log.info("cache slot %d moves to another person (theirs was the oldest message)", slot)
        self.owner[slot] = who
        self.last.setdefault(who, time.monotonic())
        return slot


class LLM:
    """One model call at a time: there is one GPU (SPEC §3.3)."""

    def __init__(self, url: str, model: str, timeout: float = 600) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=timeout)
        self.lock = asyncio.Lock()
        # Who's using the model and who's waiting for it: one request at a time on the GPU (SPEC §3.3).
        self.holder: str | None = None
        self.waiting: dict[str, int] = {}
        self.slots: Slots | None = None  # read from the server on the first call (SPEC §3.2 "Cache slots")
        self.last_wrote: dict[str, float] = {}  # person → when they last wrote: who keeps a slot

    async def _slots(self) -> Slots:
        """The server's slots, asked once; until it answers (the model still loading), the server chooses."""
        if self.slots is not None:
            return self.slots
        try:
            r = await self.client.get(self.url.removesuffix("/v1") + "/props", params={"model": self.model})
            count = int(r.json().get("total_slots", 0)) if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            count = None
        if count is None:
            return Slots(0, self.last_wrote)  # ask again next time
        self.slots = Slots(count, self.last_wrote)
        log.info("model server: %d cache slots", count)
        return self.slots

    def wrote(self, who: str) -> None:
        """The person sent a message: their slot is the newest one to keep."""
        self.last_wrote[who] = time.monotonic()

    def forget_slots(self) -> None:
        """Another model is loaded: its slots start empty, and may be a different number."""
        self.slots = None

    def queued(self, who: str) -> bool:
        """`who` is waiting for the model while someone else's request runs: their turn hasn't come yet."""
        return self.waiting.get(who, 0) > 0 and self.holder is not None and self.holder != who

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        who: str | None = None,
        tool_choice: str = "auto",
        thinking: bool = True,
        background: bool = False,
    ) -> Reply:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        if not thinking:  # only the end of the prompt changes, so the cached prefix still matches (SPEC §4.2)
            body["chat_template_kwargs"] = {"enable_thinking": False}
        slots = await self._slots()
        slot = slots.background if background else (slots.of(who) if who else None)
        if slot is not None:
            body["id_slot"] = slot
        if who:
            self.waiting[who] = self.waiting.get(who, 0) + 1
        try:
            await self.lock.acquire()
        finally:
            if who:
                self.waiting[who] -= 1
                if not self.waiting[who]:
                    del self.waiting[who]
        self.holder = who or ""
        try:
            start = time.monotonic()
            data = await self._post(body)
            ms = int((time.monotonic() - start) * 1000)
        finally:
            self.holder = None
            self.lock.release()
        msg = data["choices"][0]["message"]
        calls = [
            ToolCall(c.get("id") or f"call{i}", c["function"]["name"], c["function"].get("arguments") or "{}")
            for i, c in enumerate(msg.get("tool_calls") or [])
        ]
        usage = data.get("usage", {})
        return Reply(
            (msg.get("content") or "").strip(),
            calls,
            usage.get("prompt_tokens", 0),
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            (msg.get("reasoning_content") or "").strip(),
            ms,
            {k: v for k, v in (data.get("timings") or {}).items() if k in TIMINGS and isinstance(v, (int, float))},
        )

    async def _post(self, body: dict) -> dict:
        for attempt in range(6):
            try:
                r = await self.client.post(f"{self.url}/chat/completions", json=body)
            except httpx.TransportError as e:
                if attempt == 5:
                    raise ModelUnavailable(str(e)) from e
                await asyncio.sleep(5)  # the model may still be loading
                continue
            if r.status_code == 503 and attempt < 5:
                await asyncio.sleep(5)
                continue
            if r.status_code >= 400:
                raise ModelUnavailable(f"HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise ModelUnavailable("no answer")

    async def close(self) -> None:
        await self.client.aclose()
