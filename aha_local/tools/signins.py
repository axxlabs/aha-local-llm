# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Each person's browser state: what their browser remembers between conversations (SPEC §3.4).

The state is Playwright's storage state (every cookie, and each origin's localStorage and IndexedDB), plus the
sites the person signed in on through a sign-in card. It's kept encrypted in the account's own folder, put back
into that account's browser context when it opens and saved again after each task and before the context
closes; never into another account's context. It's read and written with fixed code of this module through
`browser_run_code_unsafe`, a tool the model can't call (SPEC §5.2).
"""

from __future__ import annotations

import json
import logging
import time

import mcp.types as mt

from ..accounts import Account
from ..approvals import MASK
from ..policy.urls import site_of

log = logging.getLogger("aha.signins")

FILE = "browser.json"
OLD_FILE = "sign-ins.json"  # before the browser kept everything: only the sites kept with "Keep me signed in"
VERSION = 1
MAX_STATE = 8 * 1024 * 1024  # bytes of JSON; larger is saved without IndexedDB, then as cookies only
TIMEOUT = 30  # seconds for reading or writing the state in the browser
GET_STATE = "async (page) => JSON.stringify(await page.context().storageState({ indexedDB: true }))"
GET_STATE_LIGHT = "async (page) => JSON.stringify(await page.context().storageState())"
GET_URL = "async (page) => page.url()"
MIN_SECRET = 3  # shorter values aren't masked in results: they'd blank out ordinary text


def redact(text: str, secrets: list[str]) -> str:
    """Hide what the person typed on a sign-in card, wherever it shows up again (SPEC §3.4)."""
    for s in sorted({s for s in secrets if len(s) >= MIN_SECRET}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return text


# ---- the file


def empty() -> dict:
    return {"v": VERSION, "state": {"cookies": [], "origins": []}, "signed_in": {}, "saved": 0}


def _migrate(account: Account) -> dict | None:
    """An old sign-ins.json becomes the first browser state: those cookies, those sites signed in."""
    old_path = account.dir / OLD_FILE
    old = account.vault.read_json(old_path, None) if old_path.exists() else None
    if old is None:
        return None
    data = empty()
    for host, entry in old.items():
        data["state"]["cookies"].extend(c for c in entry.get("cookies", []) if isinstance(c, dict))
        data["signed_in"][host] = {"site": entry.get("site", site_of(host)), "at": entry.get("saved", 0)}
    data["saved"] = int(time.time())
    save(account, data)
    old_path.unlink(missing_ok=True)
    log.info("moved kept sign-ins into the browser state (%d sites)", len(data["signed_in"]))
    return data


def load(account: Account) -> dict:
    path = account.dir / FILE
    data = account.vault.read_json(path, None) if path.exists() else None
    if data is None:
        data = _migrate(account)
    if not isinstance(data, dict):
        return empty()
    out = empty()
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    out["state"]["cookies"] = [c for c in state.get("cookies", []) if isinstance(c, dict)]
    out["state"]["origins"] = [o for o in state.get("origins", []) if isinstance(o, dict)]
    out["signed_in"] = {h: e for h, e in (data.get("signed_in") or {}).items() if isinstance(e, dict)}
    out["saved"] = data.get("saved", 0)
    if isinstance(data.get("page"), str):
        out["page"] = data["page"]
    return out


def save(account: Account, data: dict) -> None:
    path = account.dir / FILE
    state = data["state"]
    if state["cookies"] or state["origins"] or data["signed_in"]:
        account.vault.write_json(path, {**data, "v": VERSION})
    else:
        path.unlink(missing_ok=True)


def clear(account: Account) -> None:
    """Clear browser data: everything the person's browser remembers (the caller closes the open context)."""
    (account.dir / FILE).unlink(missing_ok=True)
    (account.dir / OLD_FILE).unlink(missing_ok=True)


# ---- what's in it


def matches(domain: str, site: str) -> bool:
    d = domain.lstrip(".").lower()
    return d == site or d.endswith("." + site)


def _origin_host(origin: str) -> str:
    return origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()


def _entry_site(host: str, entry: dict) -> str:
    return str(entry.get("site") or site_of(host))


def signed_in_sites(account: Account) -> set[str]:
    """The sites the person signed in on through a sign-in card (for the policy, SPEC §5.6)."""
    return {_entry_site(h, e) for h, e in load(account)["signed_in"].items()}


def last_page(account: Account) -> str:
    """The page open when the state was last saved: where a new, blank window picks up (SPEC §3.4)."""
    return load(account).get("page", "")


def sites(account: Account) -> list[dict]:
    """The sites the browser keeps data for, signed-in ones marked: [{"site", "signed_in"}], sorted."""
    data = load(account)
    names = {site_of(c["domain"].lstrip(".")) for c in data["state"]["cookies"] if isinstance(c.get("domain"), str)}
    names |= {site_of(h) for o in data["state"]["origins"] if (h := _origin_host(str(o.get("origin", ""))))}
    signed = {_entry_site(h, e) for h, e in data["signed_in"].items()}
    names = (names | signed) - {""}
    return [{"site": s, "signed_in": s in signed} for s in sorted(names)]


def site_name(text: str) -> str:
    """What the person or the app called a site ('www.waitrose.com', 'https://x.co.uk/a') → 'waitrose.com'."""
    name = text.strip().lower().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
    name = name.split(":", 1)[0].removeprefix("www.")
    return site_of(name) if "." in name else ""


def forget(account: Account, site: str) -> bool:
    """Forget one site's cookies and data (its domain and subdomains). The caller closes the open context."""
    name = site_name(site)
    if not name:
        return False
    data = load(account)
    state = data["state"]
    cookies = [c for c in state["cookies"] if not matches(str(c.get("domain", "")), name)]
    origins = [o for o in state["origins"] if not matches(_origin_host(str(o.get("origin", ""))), name)]
    signed = {h: e for h, e in data["signed_in"].items() if _entry_site(h, e) != name}
    before = (len(state["cookies"]), len(state["origins"]), len(data["signed_in"]))
    if (len(cookies), len(origins), len(signed)) == before:
        return False
    data["state"] = {"cookies": cookies, "origins": origins}
    data["signed_in"] = signed
    if matches(_origin_host(data.get("page", "")), name):
        data.pop("page")
    save(account, data)
    return True


# ---- in the browser


def _result(r: mt.CallToolResult):
    """The value a `browser_run_code_unsafe` call returned: '### Result' then a JSON value."""
    text = "\n".join(c.text for c in r.content if isinstance(c, mt.TextContent))
    if r.isError or "### Result" not in text:
        raise RuntimeError("the browser didn't run the harness code")
    body = text.split("### Result", 1)[1].split("\n### ", 1)[0].strip()
    return json.loads(body)


def _set_code(state: dict) -> str:
    # The state goes in as a JSON string literal, parsed by the page: a cookie value can't become code.
    literal = json.dumps(json.dumps(state))
    return f"async (page) => {{ await page.context().setStorageState(JSON.parse({literal})); return 'ok'; }}"


def _fresh(items: list[dict], now: float) -> list[dict]:
    return [c for c in items if not (isinstance(c.get("expires"), (int, float)) and 0 < c["expires"] < now)]


async def _read(session, code: str) -> dict:
    value = _result(await session.call("browser_run_code_unsafe", {"code": code}, timeout=TIMEOUT))
    state = json.loads(value) if isinstance(value, str) else value
    if not isinstance(state, dict):
        raise RuntimeError("the browser returned no storage state")
    return {
        "cookies": [c for c in state.get("cookies", []) if isinstance(c, dict) and isinstance(c.get("domain"), str)],
        "origins": [o for o in state.get("origins", []) if isinstance(o, dict)],
    }


def _size(state: dict) -> int:
    return len(json.dumps(state, ensure_ascii=False).encode())


async def read_state(session) -> dict:
    """The context's storage state, at most MAX_STATE: without IndexedDB if larger, then cookies only."""
    state = await _read(session, GET_STATE)
    if _size(state) <= MAX_STATE:
        return state
    log.info("browser state too large with IndexedDB; saving without it")
    state = await _read(session, GET_STATE_LIGHT)
    if _size(state) <= MAX_STATE:
        return state
    log.info("browser state still too large; saving cookies only")
    return {"cookies": state["cookies"], "origins": []}


async def restore(account: Account, session) -> int:
    """Put the person's browser state into their context that just opened. Returns how many cookies."""
    data = load(account)
    state = {"cookies": _fresh(data["state"]["cookies"], time.time()), "origins": data["state"]["origins"]}
    if state["cookies"] or state["origins"]:
        _result(await session.call("browser_run_code_unsafe", {"code": _set_code(state)}, timeout=TIMEOUT))
        log.info("restored a browser state (%d cookies, %d origins)", len(state["cookies"]), len(state["origins"]))
    return len(state["cookies"])


async def capture(account: Account, session) -> None:
    """Save the context's state as the person's browser state (after a task, before the context closes)."""
    data = load(account)
    data["state"] = await read_state(session)
    data["saved"] = int(time.time())
    try:
        url = _result(await session.call("browser_run_code_unsafe", {"code": GET_URL}, timeout=TIMEOUT))
    except Exception:
        url = ""
    if isinstance(url, str) and url.startswith(("https://", "http://")):  # a blank page keeps the last one
        data["page"] = url[:2000]
    save(account, data)


async def signed_in(account: Account, session, host: str, site: str) -> None:
    """The person signed in on `host` through a sign-in card: mark it and save the state now."""
    data = load(account)
    data["signed_in"][host] = {"site": site, "at": int(time.time())}
    save(account, data)
    await capture(account, session)
