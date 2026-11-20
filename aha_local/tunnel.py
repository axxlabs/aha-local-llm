# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The computer's side of the tunnel (SPEC §6.2): outbound only, mutual key authentication, reconnects.

Everything after the handshake is routing: end-to-end boxes and pairing messages go to the channel, which is
the only part that decides what to trust.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from . import __version__
from .accounts import Accounts
from .certs import handshake_message, public_b64, sign, verify
from .protocol import (
    E2E,
    MAX_FRAME_BYTES,
    TO_COMPUTER,
    Auth,
    CertIssued,
    DeviceStatus,
    Hello,
    Notice,
    Pair,
    PairCode,
    PairStart,
    Ping,
    Pong,
    Ready,
    b64,
)

log = logging.getLogger("aha.tunnel")


class PinMismatch(Exception):
    pass


async def open_socket(url: str):
    """ws://, wss:// or unix:/path/to/socket (development: a gateway on the same machine)."""
    if url.startswith("unix:"):
        return await websockets.unix_connect(
            url[5:], uri="ws://localhost/v1/tunnel", max_size=MAX_FRAME_BYTES, ping_interval=30
        )
    return await websockets.connect(url, max_size=MAX_FRAME_BYTES, ping_interval=30, open_timeout=15)


class Tunnel:
    def __init__(self, url: str, pin: str, key: Ed25519PrivateKey, accounts: Accounts, name: str, channel) -> None:
        self.url, self.pin, self.key = url, pin, key
        self.instance = public_b64(key)
        self.accounts = accounts
        self.name = name
        self.channel = channel
        self.ws = None
        self.connected = asyncio.Event()
        self._code_waiters: list[asyncio.Future] = []
        self._send_lock = asyncio.Lock()
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
                self.channel.on_disconnect()
            await asyncio.sleep(delay * (0.8 + random.random() * 0.4))
            delay = min(delay * 2, 60.0)

    async def _session(self) -> None:
        ws = await open_socket(self.url)
        try:
            hello = TO_COMPUTER.validate_json(await asyncio.wait_for(ws.recv(), 15))
            if not isinstance(hello, Hello) or hello.gateway != self.pin:
                raise PinMismatch
            my_nonce = b64(os.urandom(32))
            await ws.send(
                Auth(
                    instance=self.instance,
                    nonce=my_nonce,
                    certs=self.accounts.certs(),
                    name=self.name,
                    version=__version__,
                    sig=sign(self.key, handshake_message(hello.nonce, self.instance)),
                ).model_dump_json()
            )
            ready = TO_COMPUTER.validate_json(await asyncio.wait_for(ws.recv(), 15))
            if not isinstance(ready, Ready) or not verify(self.pin, handshake_message(my_nonce), ready.sig):
                raise PinMismatch
            if ready.rejected:
                log.warning("gateway rejected %d device certificate(s)", len(ready.rejected))
            self.ws = ws
            self.channel.gateway_pin = self.pin
            self.connected.set()
            devices = sum(len(a.devices) for a in self.accounts.all())
            self.status = f"connected ({devices} device{'s' if devices != 1 else ''})"
            log.info("tunnel up")
            async for raw in ws:
                await self._frame(TO_COMPUTER.validate_json(raw))
        finally:
            await ws.close()

    async def _frame(self, frame) -> None:
        match frame:
            case E2E():
                await self.channel.on_e2e(frame)
            case Pair():
                await self.channel.on_pair(frame)
            case CertIssued():
                self.channel.on_cert(frame)
            case DeviceStatus():
                await self.channel.on_device_status(frame)
            case PairCode():
                for fut in self._code_waiters:
                    if not fut.done():
                        fut.set_result(frame.code)
                self._code_waiters.clear()
            case Notice():
                log.info("gateway notice: %s", frame.code)
            case Ping():
                await self.send(Pong())
            case Pong():
                pass

    async def send(self, frame) -> None:
        if self.ws is None:
            raise ConnectionError("not connected to the gateway")
        async with self._send_lock:
            try:
                await self.ws.send(frame.model_dump_json(exclude_none=True))
            except websockets.WebSocketException as e:
                raise ConnectionError("gateway connection lost") from e

    async def pair_code(self) -> str:
        await asyncio.wait_for(self.connected.wait(), 10)
        fut = asyncio.get_running_loop().create_future()
        self._code_waiters.append(fut)
        await self.send(PairStart())
        return await asyncio.wait_for(fut, 15)
