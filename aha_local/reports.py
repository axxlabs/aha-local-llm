# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Problem reports (SPEC §7.7): built here from the transcript, shown to the person in full, sent only when
they say so.

What's in a report: the time, versions, the person's note, the chosen latest messages and, only if chosen, the
tools behind them. Never the person's name, account or device ids, keys, or images.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

MAX_COUNT = 20
MAX_NOTE = 2000
MAX_MESSAGE = 2000
MAX_TOOL_RESULT = 500
MAX_TOOL_ARGS = 500
MAX_REPORT = 60_000
DRAFT_LIFETIME = 900


@dataclass
class Draft:
    id: str
    title: str
    text: str
    expires: float = field(default_factory=lambda: time.time() + DRAFT_LIFETIME)


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def _when(ts: object) -> str:
    if isinstance(ts, int | float):
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))
    if isinstance(ts, str):
        return ts[:16].replace("T", " ")
    return "?"


def _item_lines(item: dict, states: dict[str, str]) -> list[str]:
    when = _when(item.get("ts"))
    if item.get("t") == "msg":
        who = "You" if item.get("role") == "me" else "sha"
        text = _cut(str(item.get("text", "")), MAX_MESSAGE)
        if item.get("image"):
            text += " [image left out]"
        return [f"[{when}] {who}:", *("  " + line for line in text.splitlines() or [""])]
    if item.get("t") == "approval":
        lines = [f"[{when}] sha asked for approval ({item.get('kind', '?')}): {item.get('title', '')}"]
        for f in item.get("fields", []):
            lines.append(f"  {f.get('label', '')}: {_cut(str(f.get('value', '')), MAX_MESSAGE)}")
        lines.append(f"  → {states.get(str(item.get('id')), 'not answered')}")
        if item.get("image"):
            lines.append("  [screenshot left out]")
        return lines
    return []


def _tool_lines(entry: dict) -> list[str]:
    status = "failed" if entry.get("error") else "ok"
    args = _cut(str(entry.get("args", "")), MAX_TOOL_ARGS)
    result = _cut(str(entry.get("result", "")), MAX_TOOL_RESULT)
    return [
        f"[{_when(entry.get('ts'))}] {entry.get('name', '?')} {args} → {status}",
        *("    " + line for line in result.splitlines()),
    ]


def build(
    transcript: list[dict], count: int, note: str, details: bool, versions: dict[str, str], now: float | None = None
) -> Draft:
    """The report text from the account's transcript: the last `count` messages and cards, and the tools that
    ran from the first of them on (only with `details`)."""
    count = max(1, min(MAX_COUNT, count))
    note = _cut(note, MAX_NOTE)
    states = {
        str(e["item"]["id"]): e["item"].get("state", "")
        for e in transcript
        if e.get("kind") == "item" and e["item"].get("t") == "approval_state"
    }
    shown = [
        i for i, e in enumerate(transcript) if e.get("kind") == "item" and e["item"].get("t") in ("msg", "approval")
    ][-count:]
    start = shown[0] if shown else len(transcript)
    out = [
        f"Problem report, {_when(time.time() if now is None else now)} UTC",
        " · ".join(f"{k} {v}" for k, v in versions.items() if v),
        "",
        "What went wrong:",
        *("  " + line for line in (note or "(not given)").splitlines()),
        "",
        f"The last {len(shown)} message(s):" if shown else "No messages.",
    ]
    tools: list[str] = []
    for i, e in enumerate(transcript[start:], start):
        if i in shown:
            out += _item_lines(e["item"], states)
        elif details and e.get("kind") == "tool":
            tools += _tool_lines(e)
    if details:
        out += ["", "Technical details (the tools sha used for these messages):", *(tools or ["  (none)"])]
    text = "\n".join(out)
    if len(text) > MAX_REPORT:
        text = text[: MAX_REPORT - 40] + "\n[… report cut to fit]"
    first = note.splitlines()[0] if note else "no description"
    return Draft(id=secrets.token_hex(6), title=f"Problem report: {first}"[:100], text=text)


def versions(app: str, aha: str, model: str) -> dict[str, str]:
    return {"sha app": app[:20], "sha": aha[:20], "model": model[:60]}


def parse_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 4
