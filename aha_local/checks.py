# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Checks on the model's final answer, in code (SPEC §3.5, "numbers come from sources").

1. chat_format: Markdown the app doesn't render (headings) is normalised; everything else is left as Markdown.
2. unverified: times, prices, dates, phone numbers and reference-like numbers in the answer that appear in no
   page aha opened during the task (fetch_url or the browser), in the person's own messages, in their memories
   or in emails they let it read. Search-result snippets don't count. Those numbers get a visible warning, so a
   made-up number can't pass as checked.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$", re.M)


def chat_format(text: str) -> str:
    """The app renders a safe subset of Markdown (SPEC §7.4); headings become bold lines."""
    text = HEADING.sub(r"**\1**", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# What counts as a "number that must come from a source", with the fewest digits each must have.
PATTERNS = [
    (re.compile(r"(?<![\d:])(?:[01]?\d|2[0-3])[:.][0-5]\d(?![\d:])"), 3),  # times 10:00, 9.30
    (re.compile(r"(?<![\w:.])\d{1,2}(?:[:.]\d\d)?\s?(?:am|pm)\b", re.I), 1),  # 10am, 5 pm (not "00 AM" of 10:00 AM)
    (re.compile(r"[£$€]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:GBP|USD|EUR)\b"), 1),  # prices
    (re.compile(r"(?<![\d+])(?:\+\d{1,3}[\s-]?)?(?:\(?\d{2,5}\)?[\s-]?){2,4}\d{3,4}(?!\d)"), 7),  # phone-like
]
DIGITS = re.compile(r"\d")


def _norm(s: str) -> str:
    """Compare numbers loosely: 10.00 = 10:00, '£ 212' = '£212', '07700 900123' = '07700900123'."""
    s = s.lower().replace(".", ":").replace(",", "")
    return re.sub(r"[\s()-]", "", s)


def numbers_in(text: str) -> list[str]:
    found, seen = [], set()
    for pattern, min_digits in PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(0).strip()
            if len(DIGITS.findall(value)) < min_digits:
                continue
            key = _norm(value)
            if key not in seen:
                seen.add(key)
                found.append(value)
    return found


def _variants(value: str) -> set[str]:
    n = _norm(value)
    out = {n}
    m = re.fullmatch(r"(\d{1,2})(?::(\d\d))?(am|pm)", n)
    if m:  # 10am ↔ 10:00, 5:30pm ↔ 17:30
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        out |= {f"{h}:{m.group(2) or '00'}", f"{h:02d}:{m.group(2) or '00'}"}
    m = re.fullmatch(r"(\d{1,2}):(\d\d)", n)
    if m:  # 17:00 ↔ 5pm, 10:00 ↔ 10am
        h, mm = int(m.group(1)), m.group(2)
        suffix = "pm" if h >= 12 else "am"
        h12 = h % 12 or 12
        out |= {f"{h12}{suffix}" if mm == "00" else f"{h12}:{mm}{suffix}", f"{h12}:{mm}{suffix}", f"{h:02d}:{mm}"}
    return out


def unverified(answer: str, sources: list[str]) -> list[str]:
    haystack = _norm(" ".join(sources))
    return [v for v in numbers_in(answer) if not any(x in haystack for x in _variants(v))]


WARNING_MARK = "⚠️ _Not confirmed on a page I opened"


MEMORY_REQUEST = re.compile(r"\b(forget|remember|memor\w*|save|saving|delete|remove|erase|update|change)\b", re.I)

# ---- claims the tools must back (SPEC §3.5.1): each draft that fails one goes back to the model once

FROM_SCRATCH = (
    "Then write your reply to the person from scratch: they haven't seen your draft, and never mention this check."
)


def retry(instruction: str) -> str:
    """What the model is told when a draft goes back: the problem and what to do, then the same closing."""
    return f"[aha check] {instruction} {FROM_SCRATCH}"


@dataclass(frozen=True)
class Draft:
    asked: str  # what the person said in this task
    answer: str
    called: set[str]  # tools that ran without error in this task
    can_sign_in: bool
    sign_in_page: str = ""  # the site, when the last page the model saw is a sign-in form
    sign_in_offered: bool = False  # a sign-in card was shown in this task
    challenge_page: str = ""  # the site, when the last page the model saw shows an "are you human?" check
    human_check_offered: bool = False  # a check card was shown in this task


@dataclass(frozen=True)
class Check:
    name: str
    failed: Callable[[Draft], bool]
    retry: str
    log: str


# Affirmative only: "I've forgotten", "Done, I deleted", "I'll no longer remember". "I haven't saved" and
# "I never saved" don't match, because the verb must follow "I", "I've", "I have" directly.
MEMORY_CLAIM = re.compile(
    r"\b(?:I|I've|I have|I've now|I have now|I just)\s+(?:forgot|forgotten|removed|deleted|erased|saved|"
    r"remembered|updated|stored|noted)\b|\bI(?:'ll| will) no longer remember\b",
    re.I,
)
MEMORY_WRITE_TOOLS = {"memory_add", "memory_update", "memory_delete"}


def unbacked_memory_claim(asked: str, answer: str, tools_called: set[str]) -> bool:
    """The person asked about their memories, the answer says it's done, and no memory tool ran in this task.

    Memories change only through the tools (and the person's approval), so such an answer is untrue.
    """
    return bool(MEMORY_REQUEST.search(asked) and MEMORY_CLAIM.search(answer) and not tools_called & MEMORY_WRITE_TOOLS)


MEMORY_RETRY = retry(
    "Your draft says a memory was saved, changed or forgotten, but you haven't called memory_add, memory_update or "
    "memory_delete in this task, so nothing changed. Memories are listed in the <context> block. If the person wants "
    "a change, call the tool now (they'll approve it); if not, correct your answer."
)


SIGNIN_CLAIM = re.compile(
    r"\bsign[- ]?in card\b|\bcard (?:that )?(?:has )?appeared\b|\bsign in (?:on|using|with) the card\b", re.I
)


def unbacked_signin_claim(answer: str, tools_called: set[str]) -> bool:
    """The answer points the person at a sign-in card, but browser_sign_in didn't run: there's no card."""
    return bool(SIGNIN_CLAIM.search(answer)) and "browser_sign_in" not in tools_called


SIGNIN_RETRY = retry(
    "Your draft tells the person to use a sign-in card, but you haven't called browser_sign_in in this task, so no "
    "card was shown. A card appears only when you call browser_sign_in yourself, with the references of the sign-in "
    "fields from the latest snapshot (take one if needed; if the page has only an email field first, use that alone). "
    "If you can't find the fields, say so plainly."
)


SCHEDULE_CLAIM = re.compile(
    r"\b(?:scheduled|i'?ll remind you|i will remind you|reminder (?:is )?set|set (?:a|the|your) reminder|"
    r"i'?ll (?:check|look|let you know)[^.]{0,60}\b(?:every|each|daily|tomorrow|at \d))",
    re.I,
)


def unbacked_schedule_claim(answer: str, tools_called: set[str]) -> bool:
    """The answer says something is scheduled or will be done later, but schedule_add didn't run."""
    return bool(SCHEDULE_CLAIM.search(answer)) and "schedule_add" not in tools_called


SCHEDULE_RETRY = retry(
    "Your draft says a reminder or task is scheduled, but you haven't called schedule_add in this task, so nothing "
    "is: you can't act later on your own. Call schedule_add now (a scheduled task shows the person a card to "
    "approve), or correct your answer."
)

SECRET = r"(?:password|passcode|one-time code|verification code|2fa code|security code|pin)"
PASSWORD_ASK = re.compile(
    # "tell me your password", "send me the code", or "type/share your password here / in the chat"
    rf"\b(?:tell|send|give|share|provide|text)\s+me\b[^.?!\n]{{0,40}}\b{SECRET}\b|"
    rf"\b(?:type|share|send|paste|write|post|enter|put)\s+(?:your|the)\b[^.?!\n]{{0,30}}\b{SECRET}\b"
    rf"[^.?!\n]{{0,30}}\b(?:here|in (?:the|this) chat)\b",
    re.I,
)


def asks_for_secret(answer: str) -> bool:
    """The answer asks the person to give a password or code in the chat (the sign-in card is the only way)."""
    return bool(PASSWORD_ASK.search(answer))


SECRET_RETRY = retry(
    "Your draft asks the person for a password or code in the chat. Never do that: passwords and codes go only "
    "through browser_sign_in, where they type them on a card you never see. Offer to show the sign-in card again "
    "instead, or another way forward."
)

SIGNIN_REFUSAL = re.compile(
    # "I can't log into your account", "I'm unable to add items to your cart", "that would require your login"
    r"\b(?:can'?t|cannot|can not|unable to|not able to|won'?t be able to)\b[^.?!\n]{0,40}"
    r"\b(?:log ?(?:in|into|on)|sign ?(?:in|into)|access your|"
    r"add (?:\w+ ){0,3}to (?:your |the )?(?:cart|basket|trolley))|"
    r"\b(?:require|need)s? your (?:login|credentials|password|account details)\b",
    re.I,
)


def refuses_sign_in(answer: str, tools_called: set[str], can_sign_in: bool) -> bool:
    """The answer refuses to sign in or use the person's account on a site, without having tried the sign-in card."""
    return can_sign_in and "browser_sign_in" not in tools_called and bool(SIGNIN_REFUSAL.search(answer))


SIGNIN_REFUSAL_RETRY = retry(
    "Your draft says you can't sign in or use the person's account on the site. You can: open the site, go to its "
    "sign-in page, take a snapshot and call browser_sign_in with the references of the sign-in fields. The person "
    "types their details on the card and you never see them, so there's nothing to refuse. Then carry on with the "
    "task (their basket, orders, bookings). Only if the site can't be opened or has no sign-in form, say that "
    "plainly."
)


CHECK_CLAIM = re.compile(
    # "do the check on the card", "tap the picture on the card", "solve the captcha in the app"
    r"\b(?:check|captcha|verification|human[- ]check|puzzle) card\b|"
    r"\b(?:do|complete|solve|finish|pass)\b[^.?!\n]{0,30}\b(?:check|captcha|puzzle|verification)\b[^.?!\n]{0,30}"
    r"\b(?:on|in|using) (?:the|a) (?:card|app)\b|"
    r"\btap\b[^.?!\n]{0,40}\bon (?:the|a) (?:card|screenshot)\b",
    re.I,
)


def unbacked_check_claim(answer: str, tools_called: set[str]) -> bool:
    """The answer points the person at a check card, but browser_human_check didn't run: there's no card."""
    return bool(CHECK_CLAIM.search(answer)) and "browser_human_check" not in tools_called


CHECK_RETRY = retry(
    "Your draft tells the person to do a check on a card, but you haven't called browser_human_check in this task, "
    "so no card was shown. A card appears only when you call browser_human_check yourself, with the check on the "
    "page. If there's no check on the page, say so plainly."
)


def ignores_sign_in_page(d: Draft) -> bool:
    """The task ends on a sign-in page and no sign-in card was shown: the model didn't notice, or gave up."""
    return d.can_sign_in and bool(d.sign_in_page) and not d.sign_in_offered


def ignores_challenge(d: Draft) -> bool:
    """The task ends on a page with an "are you human?" check and no check card was shown."""
    return d.can_sign_in and bool(d.challenge_page) and not d.human_check_offered


CHALLENGE_RETRY = retry(
    'The last page you saw shows an "are you human?" check, and you haven\'t shown the user a check card, so '
    "they can't do it. Call browser_human_check now: they see the page on a card and tap where to click. Only for "
    'a "press and hold" check, which the card can\'t do, tell them so and give the link.'
)


def sign_in_page_retry(site: str) -> str:
    return retry(
        f"The last page you saw is a sign-in form on {site}, and you haven't shown the user a sign-in card, so they "
        "aren't signed in there now (an earlier session can end). If the task needs their account, call "
        "browser_sign_in with the references of that page's sign-in fields (take a snapshot first if needed). If "
        "it doesn't, answer without saying they're signed in."
    )


SCREENSHOT_REQUEST = re.compile(
    r"\bscreen ?shot|\b(?:show|send) me (?:the|a|that|this) (?:page|site|website|screen|picture)\b|"
    r"\blet me see (?:it|the page|the site)\b",
    re.I,
)


def unshown_screenshot(asked: str, tools_called: set[str]) -> bool:
    """The person asked to see something, and nothing was sent to them."""
    return (
        bool(SCREENSHOT_REQUEST.search(asked))
        and "browser_navigate" in tools_called
        and ("show_screenshot" not in tools_called)
    )


SCREENSHOT_RETRY = retry(
    "The person asked to see it, but you haven't sent them anything: browser_take_screenshot only shows the page to "
    "you. Call show_screenshot to send it to their chat, and keep your answer brief."
)


# In this order; each runs at most once per task.
CLAIM_CHECKS = (
    Check(
        "memory",
        lambda d: unbacked_memory_claim(d.asked, d.answer, d.called),
        MEMORY_RETRY,
        "draft answer claimed a memory change no tool made; asking the model to fix it",
    ),
    Check(
        "signin",
        lambda d: unbacked_signin_claim(d.answer, d.called),
        SIGNIN_RETRY,
        "draft answer pointed at a sign-in card no tool showed; asking the model to call it",
    ),
    Check(
        "refusal",
        lambda d: refuses_sign_in(d.answer, d.called, d.can_sign_in),
        SIGNIN_REFUSAL_RETRY,
        "draft answer refused to sign in to a site without trying the sign-in card",
    ),
    Check(
        "secret",
        lambda d: asks_for_secret(d.answer),
        SECRET_RETRY,
        "draft answer asked for a password or code in the chat",
    ),
    Check(
        "schedule",
        lambda d: unbacked_schedule_claim(d.answer, d.called),
        SCHEDULE_RETRY,
        "draft answer claimed something was scheduled; no schedule_add ran",
    ),
    Check(
        "check",
        lambda d: unbacked_check_claim(d.answer, d.called),
        CHECK_RETRY,
        "draft answer pointed at a check card no tool showed; asking the model to call it",
    ),
    Check(
        "challenge",
        ignores_challenge,
        CHALLENGE_RETRY,
        'draft answer left the person on an "are you human?" check without a check card; asking for one',
    ),
    Check(
        "sign_in_page",
        ignores_sign_in_page,
        "",  # depends on the site: sign_in_page_retry
        "draft answer left the person on a sign-in page without a sign-in card; asking the model to show one",
    ),
    Check(
        "screenshot",
        lambda d: unshown_screenshot(d.asked, d.called),
        SCREENSHOT_RETRY,
        "the person asked to see a page and nothing was sent; asking for show_screenshot",
    ),
)


def retry_prompt(values: list[str]) -> str:
    shown = ", ".join(values[:8])
    return retry(
        f"Your draft answer contains numbers that aren't on any page you opened in this task: {shown}. "
        "Search snippets don't count. Open the page that shows them (fetch_url, or the browser if the site "
        "blocks it) and confirm them, or give the answer without them and say you couldn't confirm them."
    )


def warning(values: list[str]) -> str:
    shown = ", ".join(values[:5]) + ("…" if len(values) > 5 else "")
    return f"{WARNING_MARK}: {shown}. Please check before relying on it._"
