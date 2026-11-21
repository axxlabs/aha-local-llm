# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The tunnel client against a small fake gateway (SPEC §6.2, §11.6)."""

import asyncio
import os
from pathlib import Path

import pytest
import websockets

from aha_local.accounts import Accounts, Device
from aha_local.certs import handshake_message, issue_cert, new_key, public_b64, sign, verify
from aha_local.protocol import E2E, FROM_COMPUTER, DeviceStatus, Hello, Ready, b64
from aha_local.rest import Vault
from aha_local.tunnel import PinMismatch, Tunnel


def accounts_in(path: Path) -> Accounts:
    return Accounts(Vault(path, os.urandom(32)))


ROOT = Path(__file__).resolve().parents[1]
TAG = b64(b"\x03" * 16)


class FakeGateway:
    def __init__(self, sign_ready_with=None) -> None:
        self.key = new_key()
        self.pin = public_b64(self.key)
        self.ready_key = sign_ready_with or self.key
        self.received: asyncio.Queue = asyncio.Queue()
        self.ws = None
        self.auth = None

    async def handler(self, ws):
        self.ws = ws
        nonce = b64(os.urandom(32))
        await ws.send(Hello(gateway=self.pin, nonce=nonce, features=["report"]).model_dump_json())
        self.auth = FROM_COMPUTER.validate_json(await ws.recv())
        assert verify(self.auth.instance, handshake_message(nonce, self.auth.instance), self.auth.sig)
        routes = [c.device for c in self.auth.certs]
        await ws.send(
            Ready(sig=sign(self.ready_key, handshake_message(self.auth.nonce)), routes=routes).model_dump_json()
        )
        async for raw in ws:
            await self.received.put(FROM_COMPUTER.validate_json(raw))


class StubChannel:
    def __init__(self) -> None:
        self.frames: asyncio.Queue = asyncio.Queue()
        self.gateway_pin = ""

    async def on_e2e(self, f):
        await self.frames.put(f)

    async def on_pair(self, f):
        await self.frames.put(f)

    def on_cert(self, f):
        self.frames.put_nowait(f)

    async def on_device_status(self, f):
        await self.frames.put(f)

    def on_disconnect(self):
        pass


@pytest.fixture
async def fake(tmp_path):
    gw = FakeGateway()
    sock = str(tmp_path / "gw.sock")
    server = await websockets.unix_serve(gw.handler, sock)
    gw.url = f"unix:{sock}"
    yield gw
    server.close()


async def test_presents_device_certificates_and_routes_frames(fake, tmp_path):
    data_key = os.urandom(32)
    accounts = Accounts(Vault(tmp_path / "data", data_key))
    key = new_key()
    acct = accounts.create("Fe")
    dev = public_b64(new_key())
    cert = issue_cert(fake.key, dev, public_b64(key), acct.tag)
    accounts.add_device(
        acct, Device(dev=dev, name="Phone", ikd="", kemd="", push_key="", ratchet={}, cert=cert.model_dump())
    )
    channel = StubChannel()
    tunnel = Tunnel(fake.url, fake.pin, key, Accounts(Vault(tmp_path / "data", data_key)), "Home", channel)
    task = asyncio.create_task(tunnel.run())
    await asyncio.wait_for(tunnel.connected.wait(), 3)
    assert [c.device for c in fake.auth.certs] == [dev]
    assert tunnel.status == "connected"
    assert channel.gateway_features == frozenset({"report"})
    await fake.ws.send(E2E(dev=dev, box="AAAA").model_dump_json())
    assert (await asyncio.wait_for(channel.frames.get(), 3)) == E2E(dev=dev, box="AAAA")
    await fake.ws.send('{"t": "from_a_newer_gateway", "x": 1}')  # ignored, not fatal (SPEC §6.5)
    await fake.ws.send(DeviceStatus(dev=dev, online=True).model_dump_json())
    assert (await asyncio.wait_for(channel.frames.get(), 3)).online
    await tunnel.send(E2E(dev=dev, box="BBBB"))
    assert (await asyncio.wait_for(fake.received.get(), 3)).box == "BBBB"
    task.cancel()


async def test_wrong_pin_never_authenticates(fake, tmp_path):
    tunnel = Tunnel(
        fake.url,
        public_b64(new_key()),
        new_key(),
        accounts_in(tmp_path / "d"),
        "Home",
        StubChannel(),
    )
    with pytest.raises(PinMismatch):
        await tunnel._session()
    assert fake.auth is None  # never sent its credentials


async def test_gateway_that_cannot_sign_with_the_pinned_key_is_refused(tmp_path):
    impostor = FakeGateway(sign_ready_with=new_key())
    sock = str(tmp_path / "gw.sock")
    server = await websockets.unix_serve(impostor.handler, sock)
    tunnel = Tunnel(f"unix:{sock}", impostor.pin, new_key(), accounts_in(tmp_path / "d"), "Home", StubChannel())
    with pytest.raises(PinMismatch):
        await tunnel._session()
    server.close()


async def test_sending_while_offline_raises_connection_error(tmp_path):
    tunnel = Tunnel("unix:/nowhere", "x", new_key(), accounts_in(tmp_path / "d"), "Home", StubChannel())
    with pytest.raises(ConnectionError):
        await tunnel.send(E2E(dev=public_b64(new_key()), box="AAAA"))


@pytest.mark.parametrize("name", ["protocol.py", "certs.py"])
def test_shared_files_match_the_gateway(name):
    gateway = ROOT.parent / "aha-gateway" / "aha_gateway" / name
    if not gateway.exists():
        pytest.skip("aha-gateway is not checked out next to this repo")
    assert (ROOT / "aha_local" / name).read_text() == gateway.read_text()


async def test_pairing_through_the_tunnel_does_not_deadlock(tmp_path):
    """Regression: a pairing waits for the gateway's certificate, which comes through the same read loop."""
    from aha_local.certs import issue_cert as issue
    from aha_local.e2e import crypto as c
    from aha_local.e2e.channel import Channel
    from aha_local.protocol import CertIssued, CertRequest, Pair, PairCode

    gw = FakeGateway()
    replies: asyncio.Queue = asyncio.Queue()

    async def handler(ws):
        gw.ws = ws
        nonce = b64(os.urandom(32))
        await ws.send(Hello(gateway=gw.pin, nonce=nonce).model_dump_json())
        gw.auth = FROM_COMPUTER.validate_json(await ws.recv())
        await ws.send(Ready(sig=sign(gw.key, handshake_message(gw.auth.nonce))).model_dump_json())
        async for raw in ws:
            f = FROM_COMPUTER.validate_json(raw)
            if f.t == "pair_start":
                await ws.send(PairCode(code="ABC-DEF", expires_in=600).model_dump_json())
            elif isinstance(f, CertRequest):
                cert = issue(gw.key, f.device, gw.auth.instance, f.tag)
                await ws.send(CertIssued(id=f.id, cert=cert).model_dump_json())
            elif isinstance(f, Pair):
                await replies.put(f.body)

    sock = str(tmp_path / "gw.sock")
    server = await websockets.unix_serve(handler, sock)
    accounts = Accounts(Vault(tmp_path / "data", os.urandom(32)))
    holder = {}

    async def send(frame):
        await holder["t"].send(frame)

    channel = Channel(accounts, tmp_path / "data" / "keys", send, "https://app", "ws://gw/v1/device", "Home")
    tunnel = Tunnel(f"unix:{sock}", gw.pin, new_key(), accounts, "Home", channel)
    holder["t"] = tunnel
    task = asyncio.create_task(tunnel.run())
    await asyncio.wait_for(tunnel.connected.wait(), 3)
    acct = accounts.create("Ana")
    code = await tunnel.pair_code()
    state = channel.new_pairing(code, acct)
    await gw.ws.send(Pair(code=code, body={"t": "hello"}).model_dump_json())
    bundle = await asyncio.wait_for(replies.get(), 3)
    keys = c.DeviceKeys.generate()
    msg, pk, _ = c.device_finish(bundle, channel.identity.fingerprint(), state.offer.psk, keys)
    await gw.ws.send(Pair(code=code, body={"t": "finish", **msg}).model_dump_json())
    ok = await asyncio.wait_for(replies.get(), 5)
    assert ok["t"] == "ok"
    c.device_check_computer(pk, c.b64d(ok["mac"]))
    assert tunnel.connected.is_set() and len(acct.devices) == 1
    task.cancel()
    server.close()
