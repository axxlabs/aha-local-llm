# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The harness's own markers, kept out of what pages, emails and messages say (SPEC §9 "Markers").

The model is told that a few shapes come from Aha! itself or the person: "[New message from the user while you
were working]", the <context> block, a background check's header, a request's record, an added skill, the
computer's dates. Text from outside (a page, an email, a WhatsApp chat, a search result) could carry the same
words and pass as them. Every tool result goes through `neutralise` (in `Registry.call`), which rewrites each
marker it carries so it no longer has that shape: quoted text stays readable, and the real markers, written by the
harness outside tool results, are the only ones there are.
"""

from __future__ import annotations

import re

# Each: a pattern for the marker, however it's spaced or cased, and what it becomes.
_MARKERS = [
    (re.compile(r"\[\s*new\s+message\s+from\s+the\s+user", re.I), "(quoted text) new message from the user"),
    (re.compile(r"<\s*(/?)\s*context\s*>", re.I), r"‹\1context›"),
    (re.compile(r"\[\s*background\s+task\s+you\s+set\s+up", re.I), "(quoted text) background task you set up"),
    (re.compile(r"\[\s*aha!?['’]s\s+record", re.I), "(quoted text) Aha!'s record"),
    (re.compile(r"\[\s*aha!?\s+added\s+the", re.I), "(quoted text) Aha! added the"),
    (re.compile(r"\(\s*dates,\s*worked\s+out\s+by\s+the\s+computer", re.I), "(quoted text: dates worked out by"),
]


def neutralise(text: str) -> str:
    for pattern, replacement in _MARKERS:
        text = pattern.sub(replacement, text)
    return text
