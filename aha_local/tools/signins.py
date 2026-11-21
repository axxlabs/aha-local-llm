# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Sites a person chose to stay signed in on (SPEC §3.4 "Signing in").

Their cookies are kept encrypted in the account's own folder and put back into that account's browser context
when it opens; never into another account's. The cookies are read and written with fixed code of this module
through `browser_run_code_unsafe`, a tool the model can't call (SPEC §5.2).
"""

from __future__ import annotations

import json
import logging
import time

import mcp.types as mt

from ..accounts import Account
from ..approvals import MASK

log = logging.getLogger("aha.signins")

FILE = "sign-ins.json"
GET_COOKIES = "async (page) => JSON.stringify(await page.context().cookies())"
MIN_SECRET = 3  # shorter values aren't masked in results: they'd blank out ordinary text


def redact(text: str, secrets: list[str]) -> str:
    """Hide what the person typed on a sign-in card, wherever it shows up again (SPEC §3.4)."""
    for s in sorted({s for s in secrets if len(s) >= MIN_SECRET}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return text


def load(account: Account) -> dict:
    return account.vault.read_json(account.dir / FILE, {}) or {}


def save(account: Account, data: dict) -> None:
    path = account.dir / FILE
    if data:
        account.vault.write_json(path, data)
    else:
        path.unlink(missing_ok=True)


def sites(account: Account) -> list[str]:
    return sorted(load(account))


def matches(domain: str, site: str) -> bool:
    d = domain.lstrip(".").lower()
    return d == site or d.endswith("." + site)


def _result(r: mt.CallToolResult):
    """The value a `browser_run_code_unsafe` call returned: '### Result' then a JSON value."""
    text = "\n".join(c.text for c in r.content if isinstance(c, mt.TextContent))
    if r.isError or "### Result" not in text:
        raise RuntimeError("the browser didn't run the cookie code")
    body = text.split("### Result", 1)[1].split("\n### ", 1)[0].strip()
    return json.loads(body)


async def cookies(session) -> list[dict]:
    value = _result(await session.call("browser_run_code_unsafe", {"code": GET_COOKIES}))
    out = json.loads(value) if isinstance(value, str) else value
    return [c for c in out if isinstance(c, dict) and isinstance(c.get("domain"), str)]


def _set_code(items: list[dict]) -> str:
    # The cookies go in as a JSON string literal, parsed by the page: a cookie value can't become code.
    literal = json.dumps(json.dumps(items))
    return f"async (page) => {{ await page.context().addCookies(JSON.parse({literal})); return 'ok'; }}"


def _clear_code(keep: list[dict]) -> str:
    literal = json.dumps(json.dumps(keep))
    return (
        "async (page) => { await page.context().clearCookies(); "
        f"await page.context().addCookies(JSON.parse({literal})); return 'ok'; }}"
    )


def _fresh(items: list[dict], now: float) -> list[dict]:
    return [c for c in items if not (isinstance(c.get("expires"), (int, float)) and 0 < c["expires"] < now)]


async def keep(account: Account, session, host: str, site: str) -> int:
    """Save the cookies of the site signed in on (`host` as the card showed it). Returns how many.

    Cookies of the whole site are kept, since sign-ins often span subdomains (login.x.com, www.x.com); they only
    ever go back into this same person's browser."""
    mine = [c for c in await cookies(session) if matches(c["domain"], site)]
    data = load(account)
    data[host] = {"site": site, "cookies": mine, "saved": int(time.time())}
    save(account, data)
    return len(mine)


async def restore(account: Account, session) -> int:
    """Put the kept sites' cookies into a context that just opened."""
    data = load(account)
    items = [c for entry in data.values() for c in _fresh(entry.get("cookies", []), time.time())]
    if items:
        _result(await session.call("browser_run_code_unsafe", {"code": _set_code(items)}))
        log.info("restored kept sign-ins into a browser context (%d sites)", len(data))
    return len(items)


async def refresh(account: Account, session) -> None:
    """After a task: save the kept sites' cookies again, so sessions the site renewed stay valid."""
    data = load(account)
    if not data:
        return
    current = await cookies(session)
    changed = False
    for host, entry in data.items():
        site = entry.get("site", host)
        mine = [c for c in current if matches(c["domain"], site)]
        if mine and mine != entry.get("cookies"):
            data[host] = {"site": site, "cookies": mine, "saved": int(time.time())}
            changed = True
    if changed:
        save(account, data)


async def forget(account: Account, site: str, session=None) -> bool:
    """Stop keeping a site; also sign it out of the open context."""
    name = site.strip().lower().removeprefix("www.")
    data = load(account)
    hosts = [h for h, e in data.items() if name in (h, e.get("site"))]
    sites_gone = {data[h].get("site", h) for h in hosts}
    for h in hosts:
        del data[h]
    save(account, data)
    found = bool(hosts)
    if session is not None and found:
        keep_these = [c for c in await cookies(session) if not any(matches(c["domain"], s) for s in sites_gone)]
        _result(await session.call("browser_run_code_unsafe", {"code": _clear_code(keep_these)}))
    return found
