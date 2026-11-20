# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The model server client: OpenAI-compatible /v1/chat/completions on llama.cpp (SPEC §3.2, §3.3)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("aha.llm")


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


class LLM:
    """One model call at a time: there is one GPU (SPEC §3.3)."""

    def __init__(self, url: str, model: str, timeout: float = 600) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=timeout)
        self.lock = asyncio.Lock()

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 4096) -> Reply:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        async with self.lock:
            data = await self._post(body)
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
