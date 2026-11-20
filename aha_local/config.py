"""Configuration (SPEC §3.7).

The host keeps `~/.config/aha/config.toml`; the agent container reads the same file mounted at
`/config/config.toml`. Paths inside the container are fixed (/data, /documents, /config) and the service
addresses come from the Quadlet units, so the TOML only holds what a person chooses.
"""

from __future__ import annotations

import os
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

[household]
name = "{name}"
timezone = "{timezone}"
documents = "{documents}"

[models]
dir = "{models_dir}"

[agent]
on_it_after_seconds = 20
approval_expiry_hours = 24
max_model_calls_per_task = 40
max_task_minutes = 15

[browser]
# true: any public site opens without asking; typing and forms still need approval (SPEC §5.2).
allow_all_sites = true
"""


@dataclass(frozen=True)
class Config:
    gateway_url: str
    gateway_pin: str
    household_name: str = "Home"
    timezone: str = "Europe/London"
    documents: Path = Path.home() / "Documents" / "aha"
    models_dir: Path = Path.home() / "aha-models"
    on_it_after: float = 20
    approval_expiry: float = 24 * 3600
    max_model_calls: int = 40
    max_task_seconds: float = 15 * 60
    allow_all_sites: bool = True
    # Service addresses (inside the containers' networks; overridable for development and tests).
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("AHA_DATA_DIR", "/data")))
    documents_mount: Path = field(default_factory=lambda: Path(os.environ.get("AHA_DOCUMENTS_DIR", "/documents")))
    llm_url: str = field(default_factory=lambda: os.environ.get("AHA_LLM_URL", "http://aha-llm:8081/v1"))
    llm_model: str = field(default_factory=lambda: os.environ.get("AHA_LLM_MODEL", "qwen3.6-35b"))
    search_url: str = field(default_factory=lambda: os.environ.get("AHA_SEARCH_URL", "http://aha-search:8080"))
    browser_url: str = field(default_factory=lambda: os.environ.get("AHA_BROWSER_URL", "http://aha-browser:8931/mcp"))
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
            documents=Path(hh.get("documents", "~/Documents/aha")).expanduser(),
            models_dir=Path(models.get("dir", "~/aha-models")).expanduser(),
            on_it_after=float(agent.get("on_it_after_seconds", 20)),
            approval_expiry=float(agent.get("approval_expiry_hours", 24)) * 3600,
            max_model_calls=int(agent.get("max_model_calls_per_task", 40)),
            max_task_seconds=float(agent.get("max_task_minutes", 15)) * 60,
            allow_all_sites=bool(browser.get("allow_all_sites", True)),
        )
