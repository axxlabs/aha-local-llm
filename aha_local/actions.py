# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The person's own actions on what Aha! keeps for them (SPEC §3.4, §3.8, §5.4, §5.7, §5.8).

Forgetting a memory, a site or a saved login, trusting a site, clearing the browser, disconnecting an account,
cancelling a scheduled task: each is one function here, whether the person did it in Settings or with a chat
command, so both do the same thing and write the same audit line (`via`: "settings" or "chat"). These are the
person's own actions, never the model's: no card, and no tool reaches them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .accounts import Account
from .tools import logins, mail, schedule, signins, trusted

if TYPE_CHECKING:
    from .agent import Agent

log = logging.getLogger("aha.actions")


def memories(account: Account) -> list[dict]:
    """What Aha! remembers about the person, for Settings: id and text only."""
    return [{"id": m.get("id"), "text": m.get("text")} for m in account.load_memory()]


def forget_memory(account: Account, memory_id: str, via: str) -> bool:
    items = account.load_memory()
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
    if not mail.address(account):
        return False
    await mail.disconnect(account, email)
    account.audit({"email_disconnected": email or "all", "via": via})
    return True


def cancel_scheduled(account: Account, entry_id: str, via: str) -> bool:
    if not any(e["id"] == entry_id for e in schedule.load(account)):
        return False
    schedule.update(account, entry_id, lambda e: None)
    account.audit({"scheduled_cancelled": entry_id, "via": via})
    return True


class Browser:
    """The person's own browser (SPEC §3.4 "Browser profiles"): it keeps everything in its own profile."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    def _session(self, account: Account):
        pool = self.agent.browser
        return pool.for_account(account.id) if pool is not None else None

    async def sites(self, account: Account) -> list[dict]:
        """The sites the person's browser keeps data for, read from it."""
        try:
            return await signins.sites(account, self._session(account))
        except Exception:
            log.warning("couldn't read the browser's sites")
            return await signins.sites(account, None)

    async def listing(self, account: Account) -> list[dict]:
        """Settings → Your browser: the sites it remembers, the trusted ones, and saved logins (SPEC §5.7, §5.8)."""
        known = await self.sites(account)
        trust, saved = set(trusted.sites(account)), logins.sites(account)
        listed = {s["site"]: {**s, "trusted": s["site"] in trust} for s in known}
        for site in (trust | saved) - set(listed):
            listed[site] = {"site": site, "signed_in": False, "trusted": site in trust}
        return [{**listed[s], "login_saved": s in saved} for s in sorted(listed)]

    async def forget_site(self, account: Account, site: str, via: str) -> bool:
        """Forget one site in the person's browser (cookies and storage), and its saved logins (§5.8)."""
        name = signins.site_name(site) or site
        found = await signins.forget(account, self._session(account), site)
        found = logins.forget(account, name) or found
        if found:
            account.audit({"site_forgotten": name, "via": via})
        return found

    async def clear(self, account: Account, via: str) -> None:
        """Clear browser: stop the running task, then the person's browser, and delete their whole profile."""
        await self.agent.stop(account, quiet=True)
        if self.agent.browser is not None:
            await self.agent.browser.forget(account.id)
        signins.clear(account)
        logins.clear(account)
        account.audit({"browser_cleared": True, "via": via})
        log.info("browser cleared for an account: its profile deleted")
