# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Skills (SPEC §3.5 "Skills"): the how-to for each kind of work, out of the base prompt.

Each skill is a Markdown file in `aha_local/skills/`, shipped and reviewed like code (the model never writes one),
with a short header: `name`, `description`, and optionally `sites` (hosts) and `tools` (a tool-name prefix). The
base prompt lists them by name and description (static, so the prompt stays cached), and the model reads one with
`skill_read`. The harness also adds a skill itself, once per task, the first time the task opens one of its sites
or calls one of its tools: a site's own how-to can't be skipped by forgetting to read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

from .tools.registry import Registry, TaskState, Tool, ToolContext, ToolResult, params, string

DIR = Path(__file__).parent / "skills"
ADDED = "[sha added the {name} skill for this step]"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    sites: tuple[str, ...] = ()
    tools: str = ""


def parse(text: str) -> Skill:
    head, sep, body = text.removeprefix("---\n").partition("\n---\n")
    if not text.startswith("---\n") or not sep:
        raise ValueError("a skill starts with a --- header")
    meta = {k.strip(): v.strip() for k, _, v in (line.partition(":") for line in head.splitlines()) if k.strip()}
    return Skill(
        name=meta["name"],
        description=meta["description"],
        body=body.strip(),
        sites=tuple(s.strip().lower() for s in meta.get("sites", "").split(",") if s.strip()),
        tools=meta.get("tools", ""),
    )


@cache
def all_skills() -> dict[str, Skill]:
    skills = [parse(p.read_text()) for p in sorted(DIR.glob("*.md"))]
    return {s.name: s for s in skills}


def listing() -> str:
    """For the base prompt: one line per skill, in a fixed order."""
    return "\n".join(f"- {s.name}: {s.description}" for s in all_skills().values())


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def due(task: TaskState, tool: str) -> list[Skill]:
    """The skills the harness adds after this call: its site's or its tool's, not yet in the task."""
    host = _host(task.page_url)
    out = []
    for s in all_skills().values():
        if s.name in task.skills:
            continue
        on_site = any(host == h or host.endswith("." + h) for h in s.sites)
        if on_site or (s.tools and tool.startswith(s.tools)):
            task.skills.add(s.name)
            out.append(s)
    return out


def added(skill: Skill) -> str:
    return f"{ADDED.format(name=skill.name)}\n\n{skill.body}"


async def skill_read(args: dict, ctx: ToolContext) -> ToolResult:
    name = str(args.get("name", "")).strip().lower()
    skill = all_skills().get(name)
    if skill is None:
        return ToolResult(f"There's no skill called “{name}”. Skills: {', '.join(all_skills())}.", error=True)
    ctx.task.skills.add(name)
    return ToolResult(skill.body)


def register(reg: Registry) -> None:
    reg.add(
        Tool(
            "skill_read",
            "Read a skill: the how-to for a kind of work (the list is in your instructions). Read it before you start.",
            params(["name"], name=string("The skill's name, e.g. bookings")),
            skill_read,
        )
    )
