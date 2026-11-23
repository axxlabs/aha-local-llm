# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Configuration (SPEC §3.7).

The host keeps `~/.config/aha/config.toml`; the agent container reads the same file mounted at
`/config/config.toml`. Paths inside the container are fixed (/data, /config) and the service
addresses come from the Quadlet units, so the TOML only holds what a person chooses.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME_CONFIG = Path(os.environ.get("AHA_CONFIG", Path.home() / ".config" / "aha" / "config.toml"))
HOME_DATA = Path(os.environ.get("AHA_DATA", Path.home() / ".local" / "share" / "aha"))

DEFAULT_TOML = """\
# aha configuration (SPEC §3.7). Edit, then `aha restart`.

[gateway]
url = "{gateway_url}"
pin = "{pin}"
app = "{app_url}"          # where pairing links open the Aha! app

[household]
name = "{name}"
timezone = "{timezone}"
language = "{language}"   # the browser's language (Accept-Language); `aha install` after a change

[models]
dir = "{models_dir}"

[agent]
approval_expiry_hours = 0  # 0: a card waits until it's answered (the person checks in when they can)
max_model_calls_per_task = 40
max_task_minutes = 0       # 0: no time limit; the step limit above stops a model stuck in a loop
history_sync_days = 30     # the most a device may cache or re-sync
push_preview = false       # true: notifications show the first line of the reply

[browser]
# true: any public site opens without asking; typing and forms still need approval (SPEC §5.2).
allow_all_sites = true
idle_minutes = 1440   # the browser window closes this long after the last message or browser use (state is kept)
"""


LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")


def language_tag(value: str) -> str:
    """'en_GB.UTF-8' or 'en-GB' → 'en-GB'; anything else → 'en-GB'."""
    tag = str(value).split(".", 1)[0].split("@", 1)[0].replace("_", "-")
    parts = tag.split("-")
    tag = parts[0].lower() + (f"-{parts[1].upper()}" if len(parts) > 1 else "")
    return tag if LANGUAGE.match(tag) else "en-GB"


def host_language() -> str:
    """The computer's own language, for the browser (SPEC §3.4): its LANG, else en-GB."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value and value not in ("C", "POSIX") and not value.startswith(("C.", "POSIX.")):
            return language_tag(value)
    return "en-GB"


@dataclass(frozen=True)
class Config:
    gateway_url: str
    gateway_pin: str
    household_name: str = "Home"
    timezone: str = "Europe/London"
    language: str = "en-GB"  # the browser's language, a BCP 47 tag (SPEC §3.4)
    models_dir: Path = Path.home() / "aha-models"
    approval_expiry: float = 0  # seconds; 0: never
    max_model_calls: int = 40
    max_task_seconds: float = 0  # 0: no time limit (the step limit stops a loop)
    allow_all_sites: bool = True
    browser_idle_seconds: float = 24 * 3600  # an account's browser context closes this long after its last use
    source_url: str = "https://github.com/axxlabs/aha-local-llm"  # named in `help` (AGPL §13, SPEC §10.1)
    app_url: str = "https://app.aha.axxlabs.ai"  # where pairing links open the app (SPEC §6.4)
    device_url: str = ""  # the gateway's device endpoint, for the pairing link; derived from gateway_url if empty
    history_sync_days: int = 30
    push_preview: bool = False
    # Service addresses (inside the containers' networks; overridable for development and tests).
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("AHA_DATA_DIR", "/data")))
    llm_url: str = field(default_factory=lambda: os.environ.get("AHA_LLM_URL", "http://aha-llm:8081/v1"))
    llm_model: str = field(default_factory=lambda: os.environ.get("AHA_LLM_MODEL", "qwen3.6-35b"))
    search_url: str = field(default_factory=lambda: os.environ.get("AHA_SEARCH_URL", "http://aha-search:8080"))
    browser_url: str = field(default_factory=lambda: os.environ.get("AHA_BROWSER_URL", "http://aha-browser:8931/mcp"))
    data_key_file: Path = field(default_factory=lambda: Path(os.environ.get("AHA_DATA_KEY_FILE", "/run/aha/data-key")))
    context_tokens: int = field(default_factory=lambda: int(os.environ.get("AHA_CONTEXT_TOKENS", "131072")))

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or Path(os.environ.get("AHA_CONFIG_FILE", "/config/config.toml"))
        raw = tomllib.loads(path.read_text())
        gw, hh, models = raw.get("gateway", {}), raw.get("household", {}), raw.get("models", {})
        agent, browser = raw.get("agent", {}), raw.get("browser", {})
        return cls(
            gateway_url=os.environ.get("AHA_GATEWAY_URL", gw["url"]),
            gateway_pin=gw["pin"],
            household_name=hh.get("name", "Home"),
            timezone=hh.get("timezone", "Europe/London"),
            language=language_tag(hh.get("language") or host_language()),
            models_dir=Path(models.get("dir", "~/aha-models")).expanduser(),
            approval_expiry=float(agent.get("approval_expiry_hours", 0)) * 3600,
            max_model_calls=int(agent.get("max_model_calls_per_task", 40)),
            max_task_seconds=float(agent.get("max_task_minutes", 0)) * 60,
            allow_all_sites=bool(browser.get("allow_all_sites", True)),
            browser_idle_seconds=float(browser.get("idle_minutes", 1440)) * 60,
            source_url=str(agent.get("source_url", "https://github.com/axxlabs/aha-local-llm")),
            app_url=str(gw.get("app", "https://app.aha.axxlabs.ai")),
            device_url=str(gw.get("device_url", "")),
            history_sync_days=int(agent.get("history_sync_days", 30)),
            push_preview=bool(agent.get("push_preview", False)),
        )

    @property
    def gateway_device_url(self) -> str:
        """The URL devices connect to. For a unix-socket gateway (development), the local TCP endpoint."""
        if self.device_url:
            return self.device_url
        if self.gateway_url.startswith("unix:"):
            return "ws://127.0.0.1:8700/v1/device"
        return self.gateway_url.rsplit("/v1/", 1)[0] + "/v1/device"
