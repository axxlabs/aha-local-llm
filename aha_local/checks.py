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

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass

from .tools.registry import TaskState

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
    """A draft answer and what the checks look at: the task's own state, read directly (never copied field by
    field, so a new check needs nothing here)."""

    asked: str  # what the person said in this task
    answer: str
    task: TaskState
    can_sign_in: bool = False  # the browser is there, so browser_login is

    @property
    def called(self) -> set[str]:
        """Tools that ran without error in this task (kept by the registry)."""
        return self.task.ran


@dataclass(frozen=True)
class Check:
    name: str
    failed: Callable[[Draft], bool]
    retry: str | Callable[[Draft], str]  # what the model is told; a function when it depends on the draft
    log: str

    def retry_for(self, d: Draft) -> str:
        return self.retry(d) if callable(self.retry) else self.retry


# Affirmative only: "I've forgotten", "Done, I deleted", "I'll no longer remember". "I haven't saved" and
# "I never saved" don't match, because the verb must follow "I", "I've", "I have" directly.
MEMORY_CLAIM = re.compile(
    r"\b(?:I|I've|I have|I've now|I have now|I just)\s+(?:forgot|forgotten|removed|deleted|erased|saved|"
    r"remembered|updated|stored|noted)\b|\bI(?:'ll| will) no longer remember\b",
    re.I,
)
MEMORY_WRITE_TOOLS = {"memory_add", "memory_update", "memory_delete"}


# What does something on a site. Snapshots, navigating, scrolling and screenshots only look.
BROWSER_ACTION_TOOLS = {
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_drag",
    "browser_file_upload",
    "browser_handle_dialog",
    "browser_login",
    "browser_human_check",
}


def unbacked_memory_claim(asked: str, answer: str, tools_called: set[str]) -> bool:
    """The person asked about their memories, the answer says it's done, and no memory tool ran in this task.

    Memories change only through the tools, so such an answer is untrue. Not when a
    browser action ran: "remove the toilet paper" then "I've removed it" is about a trolley, not a memory.
    """
    return bool(
        MEMORY_REQUEST.search(asked)
        and MEMORY_CLAIM.search(answer)
        and not tools_called & (MEMORY_WRITE_TOOLS | BROWSER_ACTION_TOOLS)
    )


SITE_DONE = re.compile(
    # "added back to your trolley", "I've removed the tissue", "Done — booked", "your order is placed"
    r"\b(?:added|removed|put (?:it |them )?back|placed|booked|ordered|cancell?ed|submitted|bought|reserved|"
    r"emptied|swapped|replaced)\b",
    re.I,
)
SITE_THING = re.compile(
    r"\b(?:trolley|basket|cart|checkout|order|booking|reservation|appointment|slot|delivery|wish ?list|"
    r"favourites|favorites|form)\b",
    re.I,
)


SITE_ASK = re.compile(
    r"\b(?:add|remove|put|take|book|order|cancel|buy|delete|swap|replace|empty|reserve|submit|check ?out|"
    r"change|undo|get)\b",
    re.I,
)


def unbacked_site_claim(asked: str, answer: str, tools_called: set[str]) -> bool:
    """The person asked for a change on a site, the answer says it's done, and no browser action ran in this task.

    Only for a request: reading the trolley back ("your slot is booked for Tuesday") isn't a claim of an action."""
    return (
        bool(SITE_ASK.search(asked) and SITE_DONE.search(answer) and SITE_THING.search(answer))
        and not tools_called & BROWSER_ACTION_TOOLS
        and not tools_called & MEMORY_WRITE_TOOLS
    )


SITE_RETRY = retry(
    "Your draft says something was done on a site, but no browser action (a click, typing, a form) ran in this "
    "task, so nothing changed there. Earlier turns don't count: do it now in the browser (open the site again if "
    "the page is blank, take a snapshot, click), then check the page shows it done. If you can't, say so plainly."
)


def on_blank_page(d: Draft) -> bool:
    """The task ends on a new window's blank page: the model gave up instead of opening the site again."""
    return d.task.blank_page


BLANK_RETRY = retry(
    "The last page you saw is the blank page of a new browser window. That's not a problem: the browser opens "
    "blank after a while unused, with the user's cookies and sign-ins still in it. Open the site again with "
    "browser_navigate and finish the task. Don't tell the user a session ended or ask them to check it themselves."
)


MEMORY_RETRY = retry(
    "Your draft says a memory was saved, changed or forgotten, but you haven't called memory_add, memory_update or "
    "memory_delete in this task, so nothing changed. Memories are listed in the <context> block. If the person wants "
    "a change, call the tool now; if not, correct your answer."
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
    "is: you can't act later on your own. Call schedule_add now, or correct your answer."
)

SECRET = r"(?:password|passcode|one-time code|verification code|2fa code|security code|pin)"
PASSWORD_ASK = re.compile(
    # "tell me your password", "send me the code", or "type/share your password here / in the chat"
    rf"\b(?:tell|send|give|share|provide|text)\s+me\b[^.?!\n]{{0,40}}\b{SECRET}\b|"
    rf"\b(?:type|share|send|paste|write|post|enter|put)\s+(?:your|the)\b[^.?!\n]{{0,30}}\b{SECRET}\b"
    rf"[^.?!\n]{{0,30}}\b(?:here|in (?:the|this) chat)\b|"
    # "What password would you like to use?", "I need you to provide a password" (seen on evri.com sign-up)
    rf"\b(?:what|which)\s+{SECRET}\b[^.!\n]{{0,40}}\?|"
    rf"\bneed (?:you to |from you )?(?:provide|give|choose|pick|tell|send|share)\b[^.?!\n]{{0,30}}\b{SECRET}\b",
    re.I,
)


def asks_for_secret(answer: str) -> bool:
    """The answer asks the person to give a password or code in the chat (browser_login is the only way)."""
    return bool(PASSWORD_ASK.search(answer))


SECRET_RETRY = retry(
    "Your draft asks the person for a password or code in the chat. Never do that: on a page that asks them to "
    "sign in (or for a code, or to choose a password), call browser_login; you never see their details."
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
    """The answer refuses to sign in or use the person's account on a site, without having tried browser_login."""
    return can_sign_in and "browser_login" not in tools_called and bool(SIGNIN_REFUSAL.search(answer))


SIGNIN_REFUSAL_RETRY = retry(
    "Your draft says you can't sign in or use the person's account on the site. You can: open the site, go to its "
    "sign-in page and call browser_login. You never see their details, so there's nothing to refuse. Then carry on "
    "with the "
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
    """The answer points the person at a check, but browser_human_check didn't run: they have nothing to do it on."""
    return bool(CHECK_CLAIM.search(answer)) and "browser_human_check" not in tools_called


CHECK_RETRY = retry(
    "Your draft tells the person to do a check themselves, but you haven't called browser_human_check in this "
    "task, so they have nothing to do it on. Call browser_human_check with the check on the page. If there's no "
    "check on the page, say so plainly."
)


def ignores_sign_in_page(d: Draft) -> bool:
    """The task ends on a sign-in page and browser_login didn't run: the model didn't notice, or gave up."""
    return d.can_sign_in and bool(d.task.sign_in_page) and not d.task.login_tried


def ignores_challenge(d: Draft) -> bool:
    """The task ends on a page with an "are you human?" check and browser_human_check didn't run."""
    return d.can_sign_in and bool(d.task.challenge_page) and not d.task.human_check_offered


CHALLENGE_RETRY = retry(
    'The last page you saw shows an "are you human?" check, and you haven\'t called browser_human_check, so the '
    'user can\'t do it. Call it now: they see the page and tap where to click. Only for a "press and hold" check, '
    "which can't be done that way, tell them so and give the link."
)


def sign_in_page_retry(site: str) -> str:
    return retry(
        f"The last page you saw is a sign-in form on {site}, and you haven't called browser_login, so the user "
        "isn't signed in there now (an earlier session can end). If the task needs their account, call "
        "browser_login on that page. If it doesn't, answer without saying they're signed in."
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


# A final answer that only announces work: "Let me proceed with the account creation." Nothing runs after a final
# answer, so the person waits for something that never happens (seen on evri.com sign-up). Only in the last
# sentences, and not when the answer asks the person something.
PROMISE = re.compile(
    r"\b(?:let me(?! know)|I'?ll(?! (?:need|have to|wait))|I will(?! (?:need|have to|wait))|I'?m going to|"
    r"proceeding|I'?m about to)\b[^.!\n]{0,80}"
    r"\b(?:now|proceed|continue|go ahead|fill|create|open|start|do (?:it|that|this)|try|click|sign|book|add|"
    r"remove|set (?:it )?up|make|complete|finish|submit|search|check)\b",
    re.I,
)


OFFER = re.compile(r"\b(?:if|let me know|would you like|whenever|once you|when you)\b", re.I)


def only_promises(answer: str) -> bool:
    """The answer ends by announcing work it didn't do, and doesn't ask the person anything. An offer ("if you'd
    like more, let me know and I will continue") isn't a promise."""
    if "?" in answer:
        return False
    for sentence in re.split(r"(?<=[.!])\s+", answer.strip()[-240:]):
        if PROMISE.search(sentence) and not OFFER.search(sentence):
            return True
    return False


PROMISE_RETRY = retry(
    "Your draft says you're about to do something, but it's your final answer: nothing runs after it, so the "
    "person would wait for nothing. Do it now with the tools. If you can't, or you need something from them, say "
    "that plainly instead."
)


# In this order; each runs at most once per task.
CLAIM_CHECKS = (
    Check(
        "site",
        lambda d: unbacked_site_claim(d.asked, d.answer, d.called),
        SITE_RETRY,
        "draft answer claimed something was done on a site with no browser action; asking the model to do it",
    ),
    Check(
        "blank",
        on_blank_page,
        BLANK_RETRY,
        "draft answer ended on a new window's blank page; asking the model to open the site again",
    ),
    Check(
        "memory",
        lambda d: unbacked_memory_claim(d.asked, d.answer, d.called),
        MEMORY_RETRY,
        "draft answer claimed a memory change no tool made; asking the model to fix it",
    ),
    Check(
        "refusal",
        lambda d: refuses_sign_in(d.answer, d.called, d.can_sign_in),
        SIGNIN_REFUSAL_RETRY,
        "draft answer refused to sign in to a site without trying browser_login",
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
        "draft answer pointed the person at a check no tool showed; asking the model to call it",
    ),
    Check(
        "challenge",
        ignores_challenge,
        CHALLENGE_RETRY,
        'draft answer left the person on an "are you human?" check without browser_human_check; asking for it',
    ),
    Check(
        "sign_in_page",
        ignores_sign_in_page,
        lambda d: sign_in_page_retry(d.task.sign_in_page),
        "draft answer left the person on a sign-in page without browser_login; asking the model to call it",
    ),
    Check(
        "screenshot",
        lambda d: unshown_screenshot(d.asked, d.called),
        SCREENSHOT_RETRY,
        "the person asked to see a page and nothing was sent; asking for show_screenshot",
    ),
    # Last: the specific checks above say better what's missing ("Scheduled… I'll check" is a schedule claim).
    Check(
        "promise",
        lambda d: only_promises(d.answer),
        PROMISE_RETRY,
        "draft answer only announced work it didn't do; asking the model to do it",
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


# ---- fixed in code, after the checks (SPEC §3.5.1)

_WD = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WD_RE = r"(Mon(?:day)?|Tue(?:s|sday)?|Wed(?:nesday)?|Thu(?:r|rs|rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"
_MON = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MON_RE = (
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t|tember)?"
    r"|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
DAY_FIRST = re.compile(rf"\b{_WD_RE}\.?,?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON_RE}\b(?:\s+(\d{{4}}))?", re.I)
MONTH_FIRST = re.compile(rf"\b{_WD_RE}\.?,?\s+{_MON_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)


def _nearest(today: dt.date, month: int, day: int, year: int | None) -> dt.date | None:
    """The date meant: in the year given, else the one nearest today (a week list spans a new year)."""
    years = [year] if year else [today.year - 1, today.year, today.year + 1]
    options = []
    for y in years:
        try:
            options.append(dt.date(y, month, day))
        except ValueError:
            continue
    return min(options, key=lambda d: abs((d - today).days)) if options else None


def _as(word: str, wd: int) -> str:
    """The right weekday, written like the wrong one was (Sat → Sun, Saturday → Sunday, SAT → SUN)."""
    name = _WD[wd] if len(word.rstrip(".")) > 4 else _WD[wd][:3]  # "Tues"/"Thurs" are short forms too
    if word.isupper():
        return name.upper()
    return name.capitalize() if word[0].isupper() else name


def _wd_of(word: str) -> int:
    return next(i for i, w in enumerate(_WD) if w.startswith(word.lower().rstrip(".")[:3]))


def fix_weekdays(answer: str, today: dt.date) -> tuple[str, int]:
    """A weekday next to a date that's another day is corrected (seen: "Sat 29 Nov" for Sunday 29 November): the
    date comes from the data, the weekday is the model's own. Returns the answer and how many were fixed."""
    fixed = 0

    def repair(m: re.Match, day: str, month: str, year: str | None) -> str:
        nonlocal fixed
        word = m.group(1)
        d = _nearest(today, _MON.index(month[:3].lower()) + 1, int(day), int(year) if year else None)
        if d is None or d.weekday() == _wd_of(word):
            return m.group(0)
        fixed += 1
        return _as(word, d.weekday()) + m.group(0)[len(word) :]

    answer = DAY_FIRST.sub(lambda m: repair(m, m.group(2), m.group(3), m.group(4)), answer)
    answer = MONTH_FIRST.sub(lambda m: repair(m, m.group(3), m.group(2), m.group(4)), answer)
    return answer, fixed


# After a draft went back (a check), the model sometimes talks about it anyway, though told not to: "The earlier
# times… were guesses — I removed them". Such lines are dropped: the person never saw the draft.
ABOUT_DRAFT = re.compile(
    r"\b(?:(?:the|my)\s+(?:earlier|previous|first|last)\s+(?:answer|draft|reply|response|version|times|numbers)"
    r"|earlier\s+(?:times|numbers|figures)|(?:were|was)\s+(?:guess(?:es|ed)?|unconfirmed)"
    r"|i\s+(?:removed|dropped)\s+(?:them|those|these|the\s+\w+)\s+from\s+the\s+answer"
    r"|aha check)\b",
    re.I,
)


def drop_draft_talk(answer: str) -> str:
    lines = [line for line in answer.splitlines() if not ABOUT_DRAFT.search(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
