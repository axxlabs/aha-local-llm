# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Which model Aha! uses: one choice for the whole household, made in the app's Settings (SPEC §3.2).

The model server runs in router mode with one model loaded at a time (`--models-max 1`); a request naming
another preset of `models.ini` makes it swap. The choice is kept encrypted in the data folder, and a model is
offered only when the model server lists it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from .rest import Vault

log = logging.getLogger("aha.models")

FILE = "model.json"


@dataclass(frozen=True)
class Model:
    name: str
    note: str


# In the order the app shows them. The ids are the presets of deploy/models.ini.
MODELS: dict[str, Model] = {
    "qwen3.6-35b": Model("Qwen3.6 35B", "Best answers. Slower: on a 16 GB graphics card part of it runs from memory."),
    "qwen3.5-9b": Model("Qwen3.5 9B", "Faster replies. Fits on the graphics card; less careful on long tasks."),
    "qwen3.6-35b-3bit": Model("Qwen3.6 35B, 3-bit", "The default, smaller: faster, less careful. Being tried."),
    "gemma-4-26b": Model("Gemma 4 26B", "Google's model; mostly fits on the graphics card. Being tried."),
    "gemma-4-12b": Model("Gemma 4 12B", "Google's smaller model; fits on the graphics card. Being tried."),
}


class ModelChoice:
    def __init__(self, vault: Vault, data_dir: Path, llm, default: str) -> None:
        self.vault = vault
        self.path = data_dir / FILE
        self.llm = llm
        saved = vault.read_json(self.path, None) if self.path.exists() else None
        chosen = saved.get("model") if isinstance(saved, dict) else None
        self.llm.model = chosen if chosen in MODELS else default

    @property
    def current(self) -> str:
        return self.llm.model

    async def available(self) -> set[str]:
        """The models the model server lists (it lists the presets of models.ini)."""
        try:
            r = await self.llm.client.get(f"{self.llm.url}/models", timeout=10)
            return {m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and "id" in m}
        except (httpx.HTTPError, ValueError, AttributeError):
            return set()

    async def listing(self) -> dict:
        ready = await self.available()
        choices = [{"id": k, "name": m.name, "note": m.note, "ready": k in ready} for k, m in MODELS.items()]
        return {"t": "model", "current": self.current, "choices": choices}

    async def set(self, model: str) -> bool:
        if model not in MODELS or model not in await self.available():
            return False
        if model != self.llm.model:
            self.llm.model = model
            if hasattr(self.llm, "forget_slots"):
                self.llm.forget_slots()
            self.vault.write_json(self.path, {"model": model})
            log.info("model set to %s", model)
        return True
