# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The page the model reads (SPEC §3.4): the snapshot compact, a long page in parts, the whole one for the
harness."""

import mcp.types as mt

from aha_local.policy import addresses
from aha_local.tools.browser_page import call_with_snapshot, normalise_targets
from aha_local.tools.page_view import PART_CHARS, browser_read_more, compact, view
from aha_local.tools.registry import TaskState, ToolContext

from .conftest import person

PRODUCT = """- listitem [ref=e20]:
  - link "view product details for Kitchen Foil 30m" [ref=e21] [cursor=pointer]:
    - /url: /ecom/products/kitchen-foil/123
    - img "Kitchen Foil 30m" [ref=e22]
  - heading "Kitchen Foil 30m" [level=2] [ref=e23]
  - generic [ref=e24]: 30Metre
  - text: Item price
  - generic [ref=e25]: £5.30
  - button "Add Kitchen Foil 30m to trolley" [ref=e26] [cursor=pointer]: Add
  - checkbox "Add Kitchen Foil 30m to favourites" [checked] [ref=e27]
  - textbox "Quantity" [ref=e28]: "1"
  - 'link "Help: delivery" [ref=e29] [cursor=pointer]':
    - /url: /help"""


def page(yaml: str, url: str = "https://shop.example/foil") -> str:
    return f"### Page\n- Page URL: {url}\n### Snapshot\n```yaml\n{yaml}\n```"


def test_compact_keeps_the_words_and_a_reference_only_on_what_can_be_clicked_or_typed_into():
    assert compact(PRODUCT).splitlines() == [
        "[view product details for Kitchen Foil 30m](e21)",
        "30Metre",  # the image and the heading only repeated the name just before
        "Item price",
        "£5.30",
        "[Add Kitchen Foil 30m to trolley](e26)",
        'checkbox "Add Kitchen Foil 30m to favourites" (e27) [checked]',
        'textbox "Quantity" (e28): 1',
        "[Help: delivery](e29)",  # a YAML-quoted line
    ]


def test_a_tables_cells_stay_even_when_they_repeat():
    table = '- row "1 1" [ref=e1]:\n  - cell "1" [ref=e2]\n  - cell "1" [ref=e3]\n- text: Total £2'
    assert compact(table).splitlines() == ["1", "1", "Total £2"]


def test_a_long_page_comes_in_parts_and_the_model_reads_on():
    items = "\n".join(f'- text: "Item {i}: £{i}.00 {"x" * 80}"' for i in range(PART_CHARS // 40))
    first, parts = view(page(items))
    assert len(parts) >= 2 and "Part 1 of" in first and "browser_read_more" in first
    assert "Item 0:" in first and f"Item {PART_CHARS // 40 - 1}:" not in first
    ctx = ToolContext(None, None, None, TaskState(page_url="https://shop.example/foil", page_parts=parts))
    import asyncio

    last = asyncio.run(browser_read_more({"part": len(parts)}, ctx))
    assert f"Item {PART_CHARS // 40 - 1}:" in last.text and "the end of this page" in last.text
    assert asyncio.run(browser_read_more({"part": len(parts) + 1}, ctx)).error


class Session:
    def __init__(self, text: str) -> None:
        self.text = text

    async def call(self, name: str, args: dict) -> mt.CallToolResult:
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=self.text)])


async def test_the_harness_reads_the_whole_snapshot_and_the_model_the_compact_one(accounts):
    ctx = ToolContext(person(accounts), None, None, TaskState())
    result = await call_with_snapshot(Session(page(PRODUCT)), "browser_snapshot", {}, ctx)
    assert "[Add Kitchen Foil 30m to trolley](e26)" in result.text and "/url:" not in result.text
    assert "[ref=e26]" in ctx.task.ref_lines["e26"]  # the card checks and labels use the page's own lines
    assert "/ecom/products/kitchen-foil/123" in ctx.task.last_snapshot
    assert ctx.task.page_url == "https://shop.example/foil"
    seen: set = set()
    addresses.remember(seen, result.full or result.text)  # as the agent does: the links the page had
    assert not addresses.made_up("https://shop.example/ecom/products/kitchen-foil/123", seen)


def test_the_model_may_give_a_reference_as_the_view_writes_it():
    assert normalise_targets({"target": "(e26)"})["target"] == "e26"
