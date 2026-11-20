# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The tunnel client against a small fake gateway (SPEC §6, §11.5)."""

import asyncio
import os
from pathlib import Path

import pytest
import websockets

from aha_local.accounts import Accounts
from aha_local.certs import cert_valid, handshake_message, issue_cert, new_key, public_b64, sign, verify
from aha_local.protocol import AUTH, TO_GATEWAY, Hello, In, Out, Paired, Ready, b64
from aha_local.tunnel import PinMismatch, Tunnel

ROOT = Path(__file__).resolve().parents[1]


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
        await ws.send(Hello(gateway=self.pin, nonce=nonce).model_dump_json())
        self.auth = AUTH.validate_json(await ws.recv())
        assert verify(self.auth.instance, handshake_message(nonce, self.auth.instance), self.auth.sig)
        routes = [c.address for c in self.auth.certs if cert_valid(c, self.pin, self.auth.instance)]
        await ws.send(
            Ready(sig=sign(self.ready_key, handshake_message(self.auth.nonce)), routes=routes).model_dump_json()
        )
        async for raw in ws:
            await self.received.put(TO_GATEWAY.validate_json(raw))


@pytest.fixture
async def fake(tmp_path):
    gw = FakeGateway()
    sock = str(tmp_path / "gw.sock")
    server = await websockets.unix_serve(gw.handler, sock)
    gw.url = f"unix:{sock}"
    yield gw
    server.close()


async def test_connects_receives_and_sends(fake, tmp_path):
    inbox: asyncio.Queue = asyncio.Queue()
    accounts = Accounts(tmp_path / "data")
    tunnel = Tunnel(fake.url, fake.pin, new_key(), accounts, "Home", inbox.put)
    task = asyncio.create_task(tunnel.run())
    await asyncio.wait_for(tunnel.connected.wait(), 3)

    cert = issue_cert(fake.key, "sim:+447900000001", tunnel.instance)
    await fake.ws.send(Paired(cert=cert, name="Fe").model_dump_json())
    for _ in range(100):
        if accounts.get("sim:+447900000001"):
            break
        await asyncio.sleep(0.01)
    assert accounts.get("sim:+447900000001").cert == cert

    await fake.ws.send(In(id="m1", sender=cert.address, text="hi").model_dump_json())
    assert (await asyncio.wait_for(inbox.get(), 3)).text == "hi"
    await tunnel.send_out(Out(id="r1", to=cert.address, text="hello"))
    assert (await asyncio.wait_for(fake.received.get(), 3)).text == "hello"
    task.cancel()


async def test_certificates_are_presented_after_a_restart(fake, tmp_path):
    accounts = Accounts(tmp_path / "data")
    key = new_key()
    accounts.upsert("sim:+447900000001", cert=issue_cert(fake.key, "sim:+447900000001", public_b64(key)))
    tunnel = Tunnel(fake.url, fake.pin, key, Accounts(tmp_path / "data"), "Home", asyncio.Queue().put)
    task = asyncio.create_task(tunnel.run())
    await asyncio.wait_for(tunnel.connected.wait(), 3)
    assert [c.address for c in fake.auth.certs] == ["sim:+447900000001"]
    assert tunnel.status == "connected (1 paired)"
    task.cancel()


async def test_a_paired_certificate_that_does_not_verify_is_ignored(fake, tmp_path):
    accounts = Accounts(tmp_path / "data")
    tunnel = Tunnel(fake.url, fake.pin, new_key(), accounts, "Home", asyncio.Queue().put)
    task = asyncio.create_task(tunnel.run())
    await asyncio.wait_for(tunnel.connected.wait(), 3)
    forged = issue_cert(new_key(), "sim:+447900000001", tunnel.instance)
    await fake.ws.send(Paired(cert=forged).model_dump_json())
    await asyncio.sleep(0.1)
    assert accounts.get("sim:+447900000001") is None
    task.cancel()


async def test_wrong_pin_never_authenticates(fake, tmp_path):
    tunnel = Tunnel(fake.url, public_b64(new_key()), new_key(), Accounts(tmp_path / "d"), "Home", asyncio.Queue().put)
    with pytest.raises(PinMismatch):
        await tunnel._session()
    assert fake.auth is None  # never sent its credentials


async def test_gateway_that_cannot_sign_with_the_pinned_key_is_refused(tmp_path):
    impostor = FakeGateway(sign_ready_with=new_key())
    sock = str(tmp_path / "gw.sock")
    server = await websockets.unix_serve(impostor.handler, sock)
    tunnel = Tunnel(f"unix:{sock}", impostor.pin, new_key(), Accounts(tmp_path / "d"), "Home", asyncio.Queue().put)
    with pytest.raises(PinMismatch):
        await tunnel._session()
    server.close()


@pytest.mark.parametrize("name", ["protocol.py", "certs.py"])
def test_shared_files_match_the_gateway(name):
    gateway = ROOT.parent / "aha-gateway" / "aha_gateway" / name
    if not gateway.exists():
        pytest.skip("aha-gateway is not checked out next to this repo")
    assert (ROOT / "aha_local" / name).read_text() == gateway.read_text()
