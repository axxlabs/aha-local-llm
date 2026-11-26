# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The page as the model reads it (SPEC §3.4 "The page the model reads"): the browser's snapshot made compact,
in parts when it's long.

Playwright's snapshot gives every element a line, a reference and its address: three to four times the words of
the page (measured on waitrose.com: a search for "foil" was 34k tokens, cut at 30k characters, so the model saw
a quarter of the results and took the rest for missing). The model gets the page's words, with a reference only
on what it can click or type into, and a long page in parts it reads on with browser_read_more, never cut. The
harness keeps the whole snapshot for its own checks (refs, labels, sign-in forms, cookie banners)."""

from __future__ import annotations

import re

from ..context import CHARS_PER_TOKEN
from .registry import ToolContext, ToolResult

PART_TOKENS = 8_000  # one part of a long page
PART_CHARS = int(PART_TOKENS * CHARS_PER_TOKEN)
RECENT = 4  # lines back a repeated name is dropped from: a product's name shows in its link, image and heading

CLICKS = {"link", "button", "tab", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "treeitem"}
TOGGLES = {"checkbox", "radio", "switch"}
INPUTS = {"textbox", "searchbox", "combobox", "spinbutton", "slider", "listbox"}
# Containers whose name only repeats their contents (a row's name is its cells', a list's its items')
QUIET = {
    "generic", "group", "list", "listitem", "region", "navigation", "main", "banner", "contentinfo", "complementary",
    "article", "row", "rowgroup", "cell", "gridcell", "table", "grid", "form", "document", "dialog", "alertdialog",
    "menu", "menubar", "tablist", "tabpanel", "tree", "toolbar", "figure", "section", "search", "status", "log",
    "alert", "marquee", "timer", "note", "presentation", "none", "separator", "iframe", "paragraph", "strong",
    "emphasis", "blockquote", "caption", "code", "definition", "term", "time", "mark", "insertion", "deletion",
    "superscript", "subscript", "columnheader", "rowheader",
}  # fmt: skip
STATES = ("checked", "disabled", "selected", "expanded", "pressed", "required")
LINE = re.compile(r'^(\w+)(?: "((?:[^"\\]|\\.)*)")?((?: \[[^\]]*\])*)(?::\s?(.*))?$')
YAML = re.compile(r"```yaml\n(.*?)(?:```|\Z)", re.S)


def compact(snapshot: str) -> str:
    """A snapshot's YAML as the page's words: `[Add to trolley](e12)` for what can be clicked, `textbox "Email"
    (e1): typed` for a field, `## Heading`, and the text. Indentation and addresses dropped; a name that a line
    just before already says is dropped too."""
    out: list[str] = []
    recent: list[str] = []

    def said(words: str) -> bool:
        w = words.lower()
        return any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", r) for r in recent)

    def add(line: str, words: str, keep: bool = False) -> None:
        words = " ".join(words.split())
        if not words and not keep:
            return
        if not keep and said(words):
            return
        out.append(line)
        recent.append(words.lower())
        del recent[:-RECENT]

    for raw in snapshot.splitlines():
        s = raw.strip()
        if not s.startswith("- "):
            continue
        s = s[2:]
        if s.startswith("'") and "':" in s:  # a YAML-quoted key: 'link "a: b" [ref=e3]':
            head, _, rest = s[1:].partition("':")
            s = head.replace("''", "'") + ":" + rest
        if s.startswith("/"):  # /url:, /placeholder: an element's address or hint
            continue
        m = LINE.match(s)
        if not m:
            text = s.strip().strip('"')
            add(text, text)
            continue
        role, name, attrs, text = m.groups()
        name = (name or "").replace('\\"', '"')
        text = (text or "").strip().strip('"')
        ref = r.group(1) if (r := re.search(r"\[ref=([^\]]+)\]", attrs or "")) else ""
        states = "".join(f" [{st}]" for st in STATES if f"[{st}" in (attrs or ""))
        if role in ("cell", "gridcell", "columnheader", "rowheader"):  # a table's cells stay, repeats and all
            add(name or text, name or text, keep=bool(name or text))
        elif role == "text":
            add(text, text)
        elif role in CLICKS and ref:
            label = name or text
            add(f"[{label}]({ref}){states}", label, keep=True)
        elif role in TOGGLES and ref:
            add(f'{role} "{name or text}" ({ref}){states}', name or text, keep=True)
        elif role in INPUTS and ref:
            add(f'{role} "{name}" ({ref}){states}' + (f": {text}" if text else ""), name, keep=True)
        elif role == "heading":
            level = int(lv.group(1)) if (lv := re.search(r"\[level=(\d)\]", attrs or "")) else 2
            add("#" * level + " " + (name or text), name or text)
        elif role == "img":
            add(f"(image: {name})", name)
        elif text:
            add(text, text)
        elif name and role not in QUIET:
            add(name, name)
    return "\n".join(out)


def view(text: str) -> tuple[str, list[str]]:
    """A browser result for the model: its snapshot compact, and the parts of a long page after the first."""
    m = YAML.search(text)
    if not m:
        if len(text) > PART_CHARS:  # not a page (a find, a script's output): only the start
            return text[:PART_CHARS] + "\n[… cut to fit]", []
        return text, []
    head, tail = text[: m.start()], text[m.end() :]
    body = compact(m.group(1))
    parts = split(body, PART_CHARS)
    first = head + "```text\n" + parts[0] + "\n```" + tail
    if len(parts) > 1:
        first += more(1, len(parts))
    return first, parts


def split(body: str, size: int) -> list[str]:
    """Whole lines, about `size` characters a part."""
    parts, cur, n = [], [], 0
    for line in body.splitlines():
        if cur and n + len(line) + 1 > size:
            parts.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line[:size])
        n += len(line) + 1
    parts.append("\n".join(cur))
    return parts


def more(shown: int, total: int) -> str:
    return (
        f"\n[Part {shown} of {total} of this page. The rest isn't shown yet: read it with browser_read_more "
        f"(part {shown + 1}) before you say something isn't on the page.]"
    )


def for_model(ctx: ToolContext, result: ToolResult) -> None:
    """After the harness read the whole page (refs, sign-in forms, banners): what the model gets of it."""
    text, parts = view(result.text)
    result.text = text
    if parts:  # a page with no snapshot (a tab list, a find) leaves the parts of the last page as they were
        ctx.task.page_parts = parts


async def browser_read_more(args: dict, ctx: ToolContext) -> ToolResult:
    parts = ctx.task.page_parts
    try:
        n = int(args.get("part", 2))
    except (TypeError, ValueError):
        n = 2
    if len(parts) < 2:
        return ToolResult("The current page was shown whole: there's no more of it.", error=True)
    if not 2 <= n <= len(parts):
        return ToolResult(f"The current page has parts 1 to {len(parts)}.", error=True)
    tail = more(n, len(parts)) if n < len(parts) else f"\n[Part {n} of {n}: the end of this page.]"
    return ToolResult(f"- Page URL: {ctx.task.page_url}\n```text\n{parts[n - 1]}\n```{tail}", kind="page")
