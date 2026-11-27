# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""What aha notes about each person's browser (SPEC §3.4).

The browser itself remembers everything, in the person's own profile folder (SPEC §3.4 "Browser profiles"):
nothing is exported or put back. This module keeps only aha's note about it, encrypted in the account's folder:
the page that was open last (where a new, blank window picks up). Whether a site is signed in is the page's to
say (a session can end), so it isn't noted. The sites the browser keeps data for are read from the browser, and a
site is forgotten in the browser, with fixed code of this module through `browser_run_code_unsafe`, a tool the
model can't call (SPEC §5.2).
"""

from __future__ import annotations

import json
import logging

import mcp.types as mt

from ..accounts import Account
from ..policy.urls import site_of

log = logging.getLogger("aha.signins")

FILE = "browser.json"
VERSION = 3  # 1 also held an exported copy of the browser's storage; 2 the signed-in sites (migrations 0004)
TIMEOUT = 30  # seconds for the harness's own browser code
GET_URL = "async (page) => page.url()"
COOKIE_DOMAINS = "async (page) => JSON.stringify([...new Set((await page.context().cookies()).map((c) => c.domain))])"
# ---- aha's notes


def load(account: Account) -> dict:
    path = account.dir / FILE
    data = account.vault.read_json(path, None) if path.exists() else None
    data = data if isinstance(data, dict) else {}
    return {"page": data["page"]} if isinstance(data.get("page"), str) else {}


def save(account: Account, data: dict) -> None:
    path = account.dir / FILE
    if data.get("page"):
        account.vault.write_json(path, {"v": VERSION, **data})
    else:
        path.unlink(missing_ok=True)


def clear(account: Account) -> None:
    """With Clear browser: the notes go with the profile."""
    (account.dir / FILE).unlink(missing_ok=True)


def matches(domain: str, site: str) -> bool:
    d = domain.lstrip(".").lower()
    return d == site or d.endswith("." + site)


def last_page(account: Account) -> str:
    """The page open at the end of the last task: where a new, blank window picks up (SPEC §3.4)."""
    return load(account).get("page", "")


async def remember_page(account: Account, session) -> None:
    """After a task: note the page that's open (a blank one keeps the last)."""
    url = code_result(await session.call("browser_run_code_unsafe", {"code": GET_URL}, timeout=TIMEOUT))
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        data = load(account)
        data["page"] = url[:2000]
        save(account, data)


def site_name(text: str) -> str:
    """What the person or the app called a site ('www.waitrose.com', 'https://x.co.uk/a') → 'waitrose.com'."""
    name = text.strip().lower().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
    name = name.split(":", 1)[0].removeprefix("www.")
    return site_of(name) if "." in name else ""


# ---- in the browser


async def sites(account: Account, session) -> list[str]:
    """The sites the browser keeps cookies for, sorted."""
    names: set[str] = set()
    if session is not None:
        domains = code_result(await session.call("browser_run_code_unsafe", {"code": COOKIE_DOMAINS}, timeout=TIMEOUT))
        names = {site_of(str(d).lstrip(".")) for d in json.loads(domains)} if isinstance(domains, str) else set()
    return sorted(names - {""})


def forget_code(site: str) -> str:
    """Fixed code: the site's cookies, and its origins' storage (local storage, IndexedDB, caches, service
    workers), cleared in the person's own browser. The site goes in as a JSON string literal, never as code."""
    literal = json.dumps(json.dumps(site))
    return (
        "async (page) => { const site = JSON.parse(" + literal + "); const ctx = page.context(); "
        "const mine = (d) => { d = d.replace(/^\\./, ''); return d === site || d.endsWith('.' + site); }; "
        "const hosts = new Set([site, 'www.' + site, ...(await ctx.cookies()).map((c) => c.domain).filter(mine)"
        ".map((d) => d.replace(/^\\./, ''))]); "
        "await ctx.clearCookies({ domain: new RegExp('(^|\\\\.)' + site.replace(/\\./g, '\\\\.') + '$') }); "
        "const cdp = await ctx.newCDPSession(page); "
        "for (const h of hosts) for (const p of ['https://', 'http://']) { "
        "try { await cdp.send('Storage.clearDataForOrigin', { origin: p + h, storageTypes: 'all' }); } catch {} } "
        "await cdp.detach(); return JSON.stringify(hosts.size); }"
    )


async def forget(account: Account, session, site: str) -> bool:
    """Forget one site in the person's browser (its domain and subdomains), and aha's notes about it."""
    name = site_name(site)
    if not name:
        return False
    known = name in await sites(account, session)
    if session is not None:
        code_result(await session.call("browser_run_code_unsafe", {"code": forget_code(name)}, timeout=TIMEOUT))
    data = load(account)
    host = data.get("page", "").split("://", 1)[-1].split("/", 1)[0]
    if host and matches(host, name):
        data.pop("page")
    save(account, data)
    return known


def code_result(r: mt.CallToolResult):
    """The value a `browser_run_code_unsafe` call returned: '### Result' then a JSON value."""
    text = "\n".join(c.text for c in r.content if isinstance(c, mt.TextContent))
    if r.isError or "### Result" not in text:
        raise RuntimeError("the browser didn't run the harness code")
    body = text.split("### Result", 1)[1].split("\n### ", 1)[0].strip()
    return json.loads(body)
