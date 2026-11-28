# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Checks on the model's final answer (SPEC §3.5.1): from the task's own state, never from its words.

The person audits what the model writes (every send shows its card, every answer is theirs to read); the harness
doesn't second-guess the text with patterns. What it checks is what it knows for a fact: the page the browser
is on. A task that ends on a new window's blank page, on a sign-in form it didn't use, or on an "are you human?"
check it didn't hand to the person, goes back to the model once. The text itself is never changed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .tools.registry import TaskState

FROM_SCRATCH = (
    "Then write your reply to the person from scratch: they haven't seen your draft, and never mention this check."
)


def retry(instruction: str) -> str:
    """What the model is told when a draft goes back: the problem and what to do, then the same closing."""
    return f"[sha check] {instruction} {FROM_SCRATCH}"


@dataclass(frozen=True)
class Draft:
    """What the checks look at: the task's own state, never the answer's words."""

    task: TaskState
    can_sign_in: bool = False  # the browser is there, so browser_login and browser_human_check are


@dataclass(frozen=True)
class Check:
    name: str
    failed: Callable[[Draft], bool]
    retry: str | Callable[[Draft], str]  # what the model is told; a function when it depends on the state
    log: str

    def retry_for(self, d: Draft) -> str:
        return self.retry(d) if callable(self.retry) else self.retry


def ignores_sign_in_page(d: Draft) -> bool:
    """The task ends on a sign-in page and browser_login didn't run: the model didn't notice, or gave up."""
    return d.can_sign_in and bool(d.task.sign_in_page) and not d.task.login_tried


def sign_in_page_retry(site: str) -> str:
    return retry(
        f"The last page you saw is a sign-in form on {site}, and you haven't called browser_login, so the user "
        "isn't signed in there now (an earlier session can end). If the task needs their account, call "
        "browser_login on that page. If it doesn't, answer without saying they're signed in."
    )


def ignores_challenge(d: Draft) -> bool:
    """The task ends on a page with an "are you human?" check and browser_human_check didn't run."""
    return d.can_sign_in and bool(d.task.challenge_page) and not d.task.human_check_offered


CHALLENGE_RETRY = retry(
    'The last page you saw shows an "are you human?" check, and you haven\'t called browser_human_check, so the '
    'user can\'t do it. Call it now: they see the page and tap where to click. Only for a "press and hold" check, '
    "which can't be done that way, tell them so and give the link."
)

# In this order; each runs at most once per task.
CLAIM_CHECKS = (
    Check(
        "challenge",
        ignores_challenge,
        CHALLENGE_RETRY,
        'task ended on an "are you human?" check without browser_human_check; asking for it',
    ),
    Check(
        "sign_in_page",
        ignores_sign_in_page,
        lambda d: sign_in_page_retry(d.task.sign_in_page),
        "task ended on a sign-in page without browser_login; asking the model to call it",
    ),
)
