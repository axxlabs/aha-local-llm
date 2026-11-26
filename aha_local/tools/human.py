# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Human pace in the browser (SPEC §3.4 "Looking like a person's browser").

Before a click the mouse moves to the element in steps, with a bend and short pauses; approved typing goes one
key at a time with uneven delays; a check card's taps are clicked the same way. This is fixed harness code run
through `browser_run_code_unsafe` (a tool the model can't call, SPEC §5.2). Only validated values go into it: an
element reference that looks like one, numbers made here, and text as a JSON string literal.
"""

from __future__ import annotations

import json
import random
import re

import mcp.types as mt

REF = re.compile(r"^(?:f\d+)?e\d+$")  # Playwright MCP's snapshot references: e12, or f1e3 inside a frame
MAX_HUMAN_TYPING = 200  # longer text is filled at once: one key at a time would take too long
MAX_TAPS = 20


def is_ref(value) -> bool:
    return isinstance(value, str) and bool(REF.match(value))


def _locator(ref: str) -> str:
    if not is_ref(ref):
        raise ValueError("not an element reference")
    return json.dumps(f"aria-ref={ref}")


def _move_to_element(rng: random.Random) -> str:
    """JS (inside the harness's async function, with `el` defined): bring the mouse to el's centre.

    The bend is a point inside the element, never beside it: a button that shows only while the mouse is over
    its row (WhatsApp's "Open message options") went away when the mouse swung 40-160 px out first, and every
    click on it failed (seen 2026-11-26, editing a sent message)."""
    fx, fy = rng.uniform(-0.35, 0.35), rng.uniform(-0.35, 0.35)
    return (
        "await el.scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {}); "
        "const b = await el.boundingBox({ timeout: 3000 }).catch(() => null); "
        "if (b) { const x = b.x + b.width / 2, y = b.y + b.height / 2; "
        f"await page.mouse.move(x + b.width * {fx:.3f}, y + b.height * {fy:.3f}, {{ steps: {rng.randint(8, 18)} }}); "
        f"await page.waitForTimeout({rng.randint(40, 120)}); "
        f"await page.mouse.move(x, y, {{ steps: {rng.randint(4, 9)} }}); "
        f"await page.waitForTimeout({rng.randint(60, 220)}); }} "
    )


def move_code(ref: str, rng: random.Random | None = None) -> str:
    """Move the mouse onto the element, as a person would before clicking it (the click itself is the MCP's)."""
    rng = rng or random.Random()
    return f"async (page) => {{ const el = page.locator({_locator(ref)}); {_move_to_element(rng)}return 'ok'; }}"


NEWLINES = ("Enter", "Shift+Enter")  # what a new line in the text is typed as (Shift+Enter: messaging sites)


def type_code(ref: str, text: str, submit: bool, rng: random.Random | None = None, newline: str = "Enter") -> str:
    """Click into the field and type `text` one key at a time with uneven delays; Enter after if `submit`."""
    rng = rng or random.Random()
    if newline not in NEWLINES:
        raise ValueError("newline")
    literal = json.dumps(json.dumps(text))
    enter = (
        f"await page.waitForTimeout({rng.randint(150, 400)}); await page.keyboard.press('Enter'); " if submit else ""
    )
    return (
        f"async (page) => {{ const el = page.locator({_locator(ref)}); {_move_to_element(rng)}"
        "await el.click({ timeout: 5000 }); await el.fill(''); "
        f"for (const ch of JSON.parse({literal})) {{ "
        f"if (ch === '\\n') await page.keyboard.press('{newline}'); else await page.keyboard.type(ch); "
        "await page.waitForTimeout(40 + Math.floor(Math.random() * 110)); } "
        f"{enter}return 'ok'; }}"
    )


def parse_taps(value: str, width: int, height: int) -> list[tuple[int, int]] | None:
    """A check card's taps, 'x,y;x,y' in the screenshot's pixels. None if malformed or outside it."""
    value = value.strip()
    if not value:
        return []
    points = []
    for part in value.split(";"):
        m = re.fullmatch(r"\s*(\d{1,5})\s*,\s*(\d{1,5})\s*", part)
        if not m:
            return None
        x, y = int(m.group(1)), int(m.group(2))
        if x >= width or y >= height:
            return None
        points.append((x, y))
    return points if len(points) <= MAX_TAPS else None


def tap_code(points: list[tuple[int, int]], rng: random.Random | None = None) -> str:
    """Click the person's taps in order: move there in steps, press, release, pause."""
    rng = rng or random.Random()
    steps = []
    for x, y in points:
        dx, dy = rng.randint(-60, 60), rng.randint(-40, 40)
        steps.append(
            f"await page.mouse.move({x + dx}, {y + dy}, {{ steps: {rng.randint(6, 14)} }}); "
            f"await page.mouse.move({x}, {y}, {{ steps: {rng.randint(3, 7)} }}); "
            f"await page.waitForTimeout({rng.randint(80, 200)}); "
            f"await page.mouse.down(); await page.waitForTimeout({rng.randint(60, 140)}); await page.mouse.up(); "
            f"await page.waitForTimeout({rng.randint(300, 800)}); "
        )
    return "async (page) => { " + "".join(steps) + "return 'ok'; }"


def ran(r: mt.CallToolResult) -> bool:
    """Whether harness code ran without error. Its result is never passed on: it echoes the code, typed text too."""
    text = "\n".join(c.text for c in r.content if isinstance(c, mt.TextContent))
    return not r.isError and "### Result" in text and "### Error" not in text
