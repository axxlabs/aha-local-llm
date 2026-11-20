# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The computer's side of the tunnel (SPEC §6): outbound only, mutual key authentication, reconnects."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from . import __version__
from .accounts import Accounts
from .certs import cert_valid, handshake_message, needs_renewal, public_b64, sign, verify
from .protocol import (
    HELLO,
    READY,
    TO_COMPUTER,
    Auth,
    In,
    Notice,
    Out,
    PairCode,
    Paired,
    PairStart,
    Ping,
    Pong,
    Renew,
    Renewed,
    Typing,
    Unpair,
    b64,
)

log = logging.getLogger("aha.tunnel")

OnIn = Callable[[In], Awaitable[None]]
OnPaired = Callable[[str], Awaitable[None]]


class PinMismatch(Exception):
    pass


async def open_socket(url: str):
    """ws://, wss:// or unix:/path/to/socket (development: a gateway on the same machine)."""
    if url.startswith("unix:"):
        return await websockets.unix_connect(
            url[5:], uri="ws://localhost/v1/tunnel", max_size=2_100_000, ping_interval=30
        )
    return await websockets.connect(url, max_size=2_100_000, ping_interval=30, open_timeout=15)


class Tunnel:
    def __init__(
        self,
        url: str,
        pin: str,
        key: Ed25519PrivateKey,
        accounts: Accounts,
        name: str,
        on_in: OnIn,
        on_paired: OnPaired | None = None,
    ) -> None:
        self.url, self.pin, self.key = url, pin, key
        self.instance = public_b64(key)
        self.accounts = accounts
        self.name = name
        self.on_in = on_in
        self.on_paired = on_paired
        self.ws = None
        self.connected = asyncio.Event()
        self._pair_waiters: list[asyncio.Future] = []
        self._paired_waiters: list[asyncio.Future] = []
        self._send_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        self.status = "starting"

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._session()
                delay = 1.0
            except PinMismatch:
                self.status = (
                    "the gateway's key doesn't match the pinned key (run `aha init` again if you changed gateways)"
                )
                log.error("gateway key does not match the pin; not connecting")
                delay = 300
            except (OSError, websockets.WebSocketException, ValidationError, TimeoutError) as e:
                self.status = f"offline ({type(e).__name__})"
                log.warning("tunnel down (%s); retrying in %.0f s", type(e).__name__, delay)
            finally:
                self.connected.clear()
                self.ws = None
            await asyncio.sleep(delay * (0.8 + random.random() * 0.4))
            delay = min(delay * 2, 60.0)

    async def _session(self) -> None:
        ws = await open_socket(self.url)
        try:
            hello = HELLO.validate_json(await asyncio.wait_for(ws.recv(), 15))
            if hello.gateway != self.pin:
                raise PinMismatch
            my_nonce = b64(os.urandom(32))
            certs = [a.cert for a in self.accounts.paired()]
            await ws.send(
                Auth(
                    instance=self.instance,
                    nonce=my_nonce,
                    certs=certs,
                    name=self.name,
                    version=__version__,
                    sig=sign(self.key, handshake_message(hello.nonce, self.instance)),
                ).model_dump_json()
            )
            ready = READY.validate_json(await asyncio.wait_for(ws.recv(), 15))
            if not verify(self.pin, handshake_message(my_nonce), ready.sig):
                raise PinMismatch
            if ready.rejected:
                log.warning(
                    "gateway rejected %d pairing certificate(s); those numbers need pairing again", len(ready.rejected)
                )
            self.ws = ws
            self.connected.set()
            self.status = f"connected ({len(ready.routes)} paired)"
            log.info("tunnel up: %d routes", len(ready.routes))
            for acct in self.accounts.paired():
                if acct.cert and needs_renewal(acct.cert):
                    await self._send(Renew(cert=acct.cert))
            async for raw in ws:
                await self._frame(TO_COMPUTER.validate_json(raw))
        finally:
            await ws.close()

    async def _frame(self, frame) -> None:
        match frame:
            case In():
                # In order; agent.inbound only routes the message and starts tasks, so it returns quickly.
                try:
                    await self.on_in(frame)
                except Exception:
                    log.exception("handling an inbound message failed")
            case PairCode():
                for fut in self._pair_waiters:
                    if not fut.done():
                        fut.set_result(frame.code)
                self._pair_waiters.clear()
            case Paired():
                if not cert_valid(frame.cert, self.pin, self.instance):
                    log.warning("ignored a pairing certificate that doesn't verify")
                    return
                self.accounts.upsert(frame.cert.address, cert=frame.cert, name=frame.name)
                log.info("paired a new account")
                for fut in self._paired_waiters:
                    if not fut.done():
                        fut.set_result(frame.cert.address)
                self._paired_waiters.clear()
                if self.on_paired:
                    task = asyncio.create_task(self.on_paired(frame.cert.address))
                    self._background.add(task)
                    task.add_done_callback(self._background.discard)
            case Renewed():
                if cert_valid(frame.cert, self.pin, self.instance):
                    self.accounts.upsert(frame.cert.address, cert=frame.cert)
            case Notice():
                log.info("gateway notice: %s", frame.code)
            case Ping():
                await self._send(Pong())
            case Pong():
                pass

    async def _send(self, frame) -> None:
        if self.ws is None:
            raise ConnectionError("not connected to the gateway")
        async with self._send_lock:
            await self.ws.send(frame.model_dump_json(exclude_none=True))

    async def send_out(self, frame: Out | Typing) -> None:
        try:
            await self._send(frame)
        except (ConnectionError, websockets.WebSocketException):
            log.warning("a reply couldn't be delivered: the gateway is offline")

    async def pair(self) -> str:
        await asyncio.wait_for(self.connected.wait(), 10)
        fut = asyncio.get_running_loop().create_future()
        self._pair_waiters.append(fut)
        await self._send(PairStart())
        return await asyncio.wait_for(fut, 15)

    async def wait_paired(self, timeout: float) -> str:
        fut = asyncio.get_running_loop().create_future()
        self._paired_waiters.append(fut)
        return await asyncio.wait_for(fut, timeout)

    async def unpair(self, address: str) -> None:
        self.accounts.unpair(address)
        if self.ws is not None:
            await self._send(Unpair(address=address))
