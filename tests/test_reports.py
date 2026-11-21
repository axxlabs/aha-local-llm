# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Problem reports (SPEC §7.7, §11.10): exactly the chosen messages, tools only when asked, sent only after
the person saw the draft."""

import asyncio

from aha_local import reports
from aha_local.protocol import Report, ReportResult

from .test_channel import Rig


def transcript() -> list[dict]:
    return [
        {"kind": "item", "item": {"t": "msg", "role": "me", "text": "old secret", "seq": 1, "ts": 1}},
        {"kind": "tool", "name": "memory_list", "args": "{}", "result": "old tool", "error": False},
        {"kind": "item", "item": {"t": "msg", "role": "me", "text": "open frame.work", "seq": 2, "ts": 2}},
        {
            "kind": "tool",
            "name": "fetch_url",
            "args": '{"url": "https://frame.work"}',
            "result": "HTTP 408",
            "error": True,
        },
        {
            "kind": "item",
            "item": {
                "t": "msg",
                "role": "aha",
                "text": "It timed out.",
                "seq": 3,
                "ts": 3,
                "image": {"mime": "image/png", "b64": "AAAA"},
            },
        },
        {"kind": "answer", "text": "It timed out."},
    ]


def test_draft_has_only_the_chosen_messages_and_tools_only_when_asked():
    v = reports.versions("0.2.0", "0.2.0", "qwen")
    plain = reports.build(transcript(), 2, "The page didn't load", False, v)
    assert "open frame.work" in plain.text and "It timed out." in plain.text and "[image left out]" in plain.text
    assert "old secret" not in plain.text and "fetch_url" not in plain.text and "AAAA" not in plain.text
    assert plain.title == "Problem report: The page didn't load"
    detailed = reports.build(transcript(), 2, "", True, v)
    assert "fetch_url" in detailed.text and "HTTP 408" in detailed.text and "failed" in detailed.text
    assert "old tool" not in detailed.text and "(not given)" in detailed.text
    assert "qwen" in detailed.text and "Aha! app 0.2.0" in detailed.text
    everything = reports.build(transcript(), 99, "", False, v)
    assert "old secret" in everything.text


async def test_nothing_is_sent_without_the_draft_then_the_gateway_answers(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    await phone.send({"t": "say", "text": "hello there", "cid": "c1"})
    phone.inbox()
    await phone.send({"t": "report_send", "id": "nope"})
    assert phone.inbox() == [{"t": "report_sent", "id": "nope", "ok": False, "code": "expired"}]
    assert rig.take(Report) == []

    await phone.send({"t": "report_preview", "count": 1, "note": "it broke", "details": False, "app": "0.2.0"})
    draft = phone.inbox()[0]
    assert draft["t"] == "report_draft" and "hello there" in draft["text"] and "Ana" not in draft["text"]
    assert rig.take(Report) == []

    await phone.send({"t": "report_send", "id": draft["id"]})  # a gateway that doesn't list "report"
    assert phone.inbox() == [{"t": "report_sent", "id": draft["id"], "ok": False, "code": "reports_off"}]
    assert rig.take(Report) == []

    rig.channel.gateway_features = frozenset({"report"})
    await phone.send({"t": "report_preview", "count": 1, "note": "it broke", "details": False, "app": "0.2.0"})
    draft = phone.inbox()[0]
    await phone.send({"t": "report_send", "id": draft["id"]})
    for _ in range(20):
        await asyncio.sleep(0.01)
        sent = rig.take(Report)
        if sent:
            break
    assert sent[0].body == draft["text"] and sent[0].id == draft["id"]
    rig.channel.on_report_result(ReportResult(id=draft["id"], ok=True))
    for _ in range(20):
        await asyncio.sleep(0.01)
        box = phone.inbox()
        if box:
            break
    assert box == [{"t": "report_sent", "id": draft["id"], "ok": True}]
    await phone.send({"t": "report_send", "id": draft["id"]})  # a draft is sent once
    assert phone.inbox()[0]["ok"] is False
