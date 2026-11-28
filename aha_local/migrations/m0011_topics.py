# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Topics that wait (SPEC §4.1): the chat's numbered parts become topics, kept in `topics.json`; every chat item
gets its topic; and each background task becomes a topic already checking on its own.

- A part (numbers.json: its first item → its number) is a topic with that number; its items are those from its
  first up to the next part's. Items before the first part go to one more topic, "Earlier".
- A background task (tasks.json) becomes the topic its latest note is in, or, without a note, a new topic whose
  first message is its instruction. Its schedule, counts, last note, links and "always" move over.
- The conversation's turns belong to the topic going on (the newest), and each topic memory entry to its topic.
- Each topic gets a `topic` item at the end of the chat, so the app learns its title and state.
- `title` and `cut` items go: topics carry their titles now, and nothing folds by cuts.
"""

from __future__ import annotations

import bisect
import json
import time

from .common import accounts

FILE = "topics.json"
SHOWN = ("msg", "approval", "steps", "act")
SCHEDULE_KEYS = (
    "title",
    "prompt",
    "done_when",
    "every",
    "at",
    "tz",
    "next",
    "created",
    "checks",
    "updates",
    "last",
    "last_how",
    "last_at",
    "card",
    "links",
    "now",
)


def needed(vault) -> bool:
    return any(not (a / FILE).exists() for a in accounts(vault))


def _line(entry: dict) -> bytes:
    return json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}, ensure_ascii=False).encode()


def _records(vault, path) -> list[dict]:
    out = []
    for line in vault.records(path) if path.exists() else []:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_raw": line})
    return out


def run(vault) -> None:
    for folder in accounts(vault):
        if (folder / FILE).exists():
            continue
        _account(vault, folder)


def _account(vault, folder) -> None:
    now = int(time.time())
    meta = vault.read_json(folder / "account.json", {}) or {}
    seq = int(meta.get("seq", 0))
    path = folder / "transcript.jsonl"
    records = _records(vault, path)
    items = [r["item"] for r in records if r.get("kind") == "item" and isinstance(r.get("item"), dict)]
    numbers = vault.read_json(folder / "numbers.json", None) or {}
    parts = {int(k): int(v) for k, v in (numbers.get("parts") or {}).items()}
    nxt = int(numbers.get("next", 1))
    starts = sorted(parts)
    titles = {it["from"]: str(it.get("text", "")) for it in items if it.get("t") == "title" and "from" in it}
    shown = [it for it in items if it.get("t") in SHOWN and isinstance(it.get("seq"), int)]

    topics: dict[int, dict] = {}

    def topic(n: int, first: int, title: str, ts: int) -> dict:
        if n not in topics:
            topics[n] = {"n": n, "title": title[:60], "first": first, "created": ts, "last": ts, "state": ""}
        return topics[n]

    for start, n in parts.items():
        topic(n, start, titles.get(start, ""), 0)
    earlier = 0
    for it in shown:
        k = bisect.bisect_right(starts, it["seq"]) - 1
        if k >= 0:
            n = parts[starts[k]]
        else:
            if not earlier:
                earlier, nxt = nxt, nxt + 1
                topic(earlier, it["seq"], "Earlier", int(it.get("ts", now)))
            n = earlier
        it["topic"] = n
        e = topics[n]
        ts = int(it.get("ts", now))
        e["last"] = max(e["last"], ts)
        e["created"] = e["created"] or ts
    for e in topics.values():
        e["created"] = e["created"] or now
        e["last"] = e["last"] or e["created"]

    # Background tasks become topics checking on their own.
    added: list[dict] = []
    conv = vault.read_json(folder / "context.json", None) or {"turns": [], "topics": []}
    conv.setdefault("turns", [])
    conv.setdefault("topics", [])
    tasks = vault.read_json(folder / "tasks.json", None) or []
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict) or not task.get("title"):
            continue
        noted = [it for it in shown if isinstance(it.get("task"), dict) and it["task"].get("id") == task.get("id")]
        if noted:
            n = noted[-1]["topic"]
        else:
            n, nxt = nxt, nxt + 1
            seq += 1
            first = {
                "t": "msg",
                "role": "aha",
                "text": f"Checking on its own: {task['title']}\n\n{task.get('prompt', '')}",
                "topic": n,
                "seq": seq,
                "ts": now,
            }
            added.append(first)
            topic(n, seq, task["title"], now)
            conv["turns"] += [
                {"role": "user", "content": task.get("prompt", ""), "ts": "", "t": now, "topic": n},
                {
                    "role": "assistant",
                    "content": f"Checking on its own: {task['title']}",
                    "ts": "",
                    "t": now,
                    "topic": n,
                },
            ]
        e = topics[n]
        e["schedule"] = {k: task[k] for k in SCHEDULE_KEYS if k in task}
        e["state"] = "scheduled"
        if task.get("allowed"):
            e["allowed"] = sorted(set(task["allowed"]))
        if not e.get("title"):
            e["title"] = task["title"][:60]

    # The conversation: its turns belong to the topic going on (the newest); each memory entry to its topic. With
    # no chat items to go by, they go to one topic of their own, "Earlier conversation".
    newest = max((it["topic"] for it in shown), default=0)

    def fallback() -> int:
        nonlocal newest, nxt
        if not newest:
            newest, nxt = nxt, nxt + 1
            topic(newest, 0, "Earlier conversation", now)
        return newest

    for t in conv["turns"]:
        if not isinstance(t.get("topic"), int):
            t["topic"] = fallback()
    entries = []
    for entry in conv["topics"]:
        start = entry.get("from")
        if isinstance(start, int) and starts:
            k = bisect.bisect_right(starts, start) - 1
            n = parts[starts[k]] if k >= 0 else (earlier or fallback())
        else:
            n = fallback()
        entry = {k: v for k, v in entry.items() if k != "from"}
        entries = [x for x in entries if x["n"] != n]
        entries.append({**entry, "id": f"t{n}", "n": n})
    conv["topics"] = entries

    # Each topic's `topic` item, at the end, so the app learns its title and state.
    for n in sorted(topics):
        e = topics[n]
        seq += 1
        out = {"t": "topic", "topic": n, "title": e["title"], "state": e["state"], "last": e["last"]}
        if s := e.get("schedule"):
            out["schedule"] = {
                "waiting_for": s.get("title", ""),
                "prompt": s.get("prompt", ""),
                "done_when": s.get("done_when", ""),
                "every": int(s.get("every", 0)) * 60,
                **({"at": s["at"]} if s.get("at") else {}),
                "next": s.get("next", 0),
                "checks": s.get("checks", 0),
                "paused": False,
            }
        added.append({**out, "seq": seq, "ts": now})

    kept = []
    for r in records:
        if "_raw" in r:
            kept.append(r["_raw"])
            continue
        item = r.get("item") if r.get("kind") == "item" else None
        if isinstance(item, dict) and item.get("t") in ("title", "cut"):
            continue
        kept.append(json.dumps(r, ensure_ascii=False).encode())
    kept += [_line({"kind": "item", "item": it}) for it in added]
    if kept or path.exists():
        vault.rewrite(path, kept)
    vault.write_json(folder / "context.json", conv)
    vault.write_json(folder / FILE, {"next": nxt, "topics": {str(n): e for n, e in sorted(topics.items())}})
    if meta and seq != int(meta.get("seq", 0)):
        vault.write_json(folder / "account.json", {**meta, "seq": seq})
    (folder / "numbers.json").unlink(missing_ok=True)
    (folder / "tasks.json").unlink(missing_ok=True)
