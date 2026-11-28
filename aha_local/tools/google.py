# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""A person's Google connections (SPEC §3.4 "Connections"), for email and calendar.

Each connection is a provider, an address and its refresh token, kept encrypted in the account's own folder and
never shown to the model. The tools reach Google only through `request`, which picks the running account's
connection, gets an access token and forgets it when Google says it's no longer valid.
"""

from __future__ import annotations

import logging
import re
import time

import httpx

from ..vault import Vault
from .registry import ToolContext

log = logging.getLogger("aha.google")

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",  # tools/calendar.py
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",  # which calendars, by name
)
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests put a fake Google here

NOT_CONNECTED = (
    "Email isn't connected for this person yet. Explain it in plain words: whoever set up sha on the computer "
    "connects it there, once for the household: a free Google Cloud app (about 15 minutes; the steps are in "
    "`aha mail --help` and the Email section of the README at https://github.com/axxlabs/aha-local-llm#email-gmail), "
    "then `aha mail connect <their name>` on the computer while this person signs in with Google. Their mail stays "
    "between Google and this computer. Don't suggest any other way to connect."
)

_tokens: dict[tuple[str, str], tuple[str, float]] = {}  # (account id, address) -> (access token, expiry), in memory

EMAIL = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+")  # one address, as a guest or a recipient (fullmatch)

FILE = "mail.json"
PROVIDERS = ("gmail",)  # SPEC §3.4 "Connections": each connection names its provider; only Gmail for now


class NotConnected(Exception):
    pass


def client_file(vault: Vault):
    return vault.root / "keys" / "google-oauth.json"


# ---- a person's connections (SPEC §3.4 "Connections"): several, each a provider, an address and its token


def connections(account) -> list[dict]:
    """The person's connected accounts, in the order they were connected. Never shown to the model."""
    path = account.dir / FILE
    data = account.vault.read_json(path, None) if path.exists() else None
    if isinstance(data, dict) and data.get("refresh_token"):  # before several: one connection on its own
        data = {"connections": [data]}
    found = data.get("connections", []) if isinstance(data, dict) else []
    return [
        c
        for c in found
        if isinstance(c, dict) and c.get("refresh_token") and c.get("email") and c.get("provider", "gmail") in PROVIDERS
    ]


def _save(account, found: list[dict]) -> None:
    path = account.dir / FILE
    if found:
        account.vault.write_json(path, {"connections": found})
    else:
        path.unlink(missing_ok=True)


def add_connection(account, provider: str, email: str, refresh_token: str) -> None:
    """Connect an account; connecting the same address again replaces its token (new permissions)."""
    kept = [c for c in connections(account) if c["email"].lower() != email.lower()]
    _save(account, [*kept, {"provider": provider, "email": email, "refresh_token": refresh_token}])
    forget_token(account.id, email)


def addresses(account) -> list[str]:
    """The connected addresses (Settings → Connections, `aha mail status`). Never a token."""
    return [c["email"] for c in connections(account)]


def address(account) -> str:
    """The first connected address, or ""."""
    found = addresses(account)
    return found[0] if found else ""


def pick(account, mailbox: str = "") -> dict | None:
    """The connection for an address the model or the person named (exact, or a single partial match), or the
    first one when none was named."""
    conns = connections(account)
    wanted = mailbox.strip().lower()
    if not wanted:
        return conns[0] if conns else None
    for match in (
        [c for c in conns if c["email"].lower() == wanted],
        [c for c in conns if wanted in c["email"].lower()],
    ):
        if len(match) == 1:
            return match[0]
    return None


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=TRANSPORT, timeout=20)


async def access_token(ctx: ToolContext, conn: dict | None = None) -> str:
    conn = conn or pick(ctx.account)
    if not conn:
        raise NotConnected
    key = (ctx.account.id, conn["email"].lower())
    cached = _tokens.get(key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    client = ctx.account.vault.read_json(client_file(ctx.account.vault)) or {}
    async with _http() as http:
        r = await http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": conn["refresh_token"],
                "client_id": client.get("client_id", ""),
                "client_secret": client.get("client_secret", ""),
            },
        )
    if r.status_code != 200:
        raise NotConnected
    body = r.json()
    _tokens[key] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
    return body["access_token"]


def forget_token(account_id: str, email: str = "") -> None:
    for key in [k for k in _tokens if k[0] == account_id and (not email or k[1] == email.lower())]:
        _tokens.pop(key, None)


async def disconnect(account, email: str = "") -> bool:
    """Revoke one connection's token at Google (every one, without an address) and delete it here. True if Google
    confirmed every revocation."""
    conns = connections(account)
    gone = [c for c in conns if not email or c["email"].lower() == email.lower()]
    if not gone:
        return False
    revoked = True
    for conn in gone:
        forget_token(account.id, conn["email"])
        try:
            async with _http() as http:
                r = await http.post(REVOKE_URL, data={"token": conn["refresh_token"]})
                revoked = revoked and r.status_code == 200
        except httpx.HTTPError:
            revoked = False
            log.warning("couldn't reach Google to revoke a mail token; deleted it here")
    _save(account, [c for c in conns if c not in gone])
    return revoked


async def request(ctx, method: str, url: str, conn: dict | None = None, **kw) -> httpx.Response:
    """A request to a Google API as the running account (its first connection by default). Raises NotConnected."""
    conn = conn or pick(ctx.account)
    if conn is None:
        raise NotConnected
    token = await access_token(ctx, conn)
    async with _http() as http:
        r = await http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
    if r.status_code == 401:
        forget_token(ctx.account.id, conn["email"])
    return r
