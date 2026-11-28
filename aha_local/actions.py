# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The person's own actions on what sha keeps for them (SPEC §3.4, §3.8, §5.4, §5.7, §5.8).

Forgetting a memory, a site or a saved login, trusting a site, clearing the browser, disconnecting an account,
stopping or changing a topic's schedule: each is one function here, whether the person did it in Settings or with a chat
command, so both do the same thing and write the same audit line (`via`: "settings" or "chat"). These are the
person's own actions, never the model's: no card, and no tool reaches them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import context
from . import topics as topic_list
from .accounts import Account
from .tools import background, google, logins, signins, trusted

if TYPE_CHECKING:
    from .agent import Agent

log = logging.getLogger("aha.actions")


def memories(account: Account) -> list[dict]:
    """The person's profile facts, for the app: id and text only."""
    return [{"id": m.get("id"), "text": m.get("text")} for m in account.load_memory()]


def topics(account: Account) -> list[dict]:
    """The topic memory, newest first, for the app (SPEC §5.4)."""
    entries = sorted(account.load_context()["topics"], key=lambda e: e.get("ts", 0), reverse=True)
    return [{"id": e["id"], "title": e.get("title", ""), "text": e["text"], "ts": int(e.get("ts", 0))} for e in entries]


def forget_memory(account: Account, memory_id: str, via: str) -> bool:
    """A profile fact, or a topic's entry (ids starting with "t")."""
    items = account.load_memory()
    if memory_id.startswith("t") and context.forget_topic(account, memory_id):
        account.audit({"memory_forgotten": memory_id, "via": via})
        return True
    if not memory_id or not any(m.get("id") == memory_id for m in items):
        return False
    account.save_memory([m for m in items if m.get("id") != memory_id])
    account.audit({"memory_forgotten": memory_id, "via": via})
    return True


def forget_login(account: Account, site: str, via: str) -> bool:
    name = signins.site_name(site) or site
    if not logins.forget(account, name):
        return False
    account.audit({"login_forgotten": name, "via": via})
    return True


def set_trust(account: Account, site: str, on: bool, via: str) -> bool:
    if not trusted.set_trusted(account, site, on):
        return False
    account.audit({"trusted_site" if on else "untrusted_site": trusted.valid(site), "via": via})
    return True


async def disconnect_email(account: Account, email: str, via: str) -> bool:
    """One connected address; every one when none is given (older apps). False if there was nothing to do."""
    if not google.address(account):
        return False
    await google.disconnect(account, email)
    account.audit({"email_disconnected": email or "all", "via": via})
    return True


def delete_topic(account: Account, n: int, via: str) -> bool:
    """The person's Delete topic (SPEC §4.1): it goes with its messages, its turns, its memory entry and whatever it
    was checking on. False if there's no such topic."""
    if topic_list.remove(account, n) is None:
        return False
    account.forget_topic(n)
    conv = account.load_context()
    conv["turns"] = [t for t in conv["turns"] if t.get("topic") != n]
    conv["topics"] = [e for e in conv["topics"] if e.get("n") != n]
    account.save_context(conv)
    account.audit({"topic_deleted": n, "via": via})
    return True


def stop_topic(account: Account, n: int, via: str) -> bool:
    """The topic's Stop: it no longer checks on its own (SPEC §4.1)."""
    if background.stop(account, n) is None:
        return False
    account.audit({"topic_schedule_stopped": n, "via": via})
    return True


def edit_topic(account: Account, n: int, changes: dict, via: str) -> str:
    """ "" when the topic's schedule was changed, else why not."""
    if why := background.edit(account, n, changes):
        return why
    account.audit({"topic_schedule_edited": n, "fields": sorted(changes), "via": via})
    return ""


def run_topic_now(account: Account, n: int, via: str) -> bool:
    if not background.run_now(account, n):
        return False
    account.audit({"topic_run_now": n, "via": via})
    return True


class Browser:
    """The person's own browser (SPEC §3.4 "Browser profiles"): it keeps everything in its own profile."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    def _session(self, account: Account):
        pool = self.agent.browser
        return pool.for_account(account.id) if pool is not None else None

    async def sites(self, account: Account) -> list[str]:
        """The sites the person's browser keeps data for, read from it."""
        try:
            return await signins.sites(account, self._session(account))
        except Exception:
            log.warning("couldn't read the browser's sites")
            return await signins.sites(account, None)

    async def listing(self, account: Account) -> list[dict]:
        """Settings → Your browser: the sites it remembers, the trusted ones, and saved logins (SPEC §5.7, §5.8)."""
        known, trust, saved = set(await self.sites(account)), set(trusted.sites(account)), logins.sites(account)
        return [{"site": s, "trusted": s in trust, "login_saved": s in saved} for s in sorted(known | trust | saved)]

    async def forget_site(self, account: Account, site: str, via: str) -> bool:
        """Forget everything about one site (SPEC §3.4): the browser's cookies and storage, its saved logins
        (§5.8) and its trust (§5.7). One Forget, one site's whole record."""
        name = signins.site_name(site) or site
        found = await signins.forget(account, self._session(account), site)
        found = logins.forget(account, name) or found
        found = (name in trusted.sites(account) and trusted.set_trusted(account, name, False)) or found
        if found:
            account.audit({"site_forgotten": name, "via": via})
        return found

    async def clear(self, account: Account, via: str) -> None:
        """Clear browser: stop the running task, then the person's browser, and delete their whole profile: every
        site forgotten, its saved logins and trust too."""
        await self.agent.stop(account, quiet=True)
        if self.agent.browser is not None:
            await self.agent.browser.forget(account.id)
        signins.clear(account)
        logins.clear(account)
        trusted.clear(account)
        account.audit({"browser_cleared": True, "via": via})
        log.info("browser cleared for an account: its profile deleted")
