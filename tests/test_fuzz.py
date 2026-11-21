# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Random and mutated inputs against everything that parses untrusted data (SPEC §9): each must fail cleanly
(CryptoError, ValidationError, or a silent drop), never crash, hang or change state."""

import contextlib
import json
import os
import random

import pytest
from pydantic import ValidationError

from aha_local.e2e import crypto as c
from aha_local.protocol import TO_COMPUTER

from .test_channel import Rig
from .test_e2e_crypto import pair

RNG = random.Random(1234)


def mutate(data: bytes) -> bytes:
    b = bytearray(data)
    for _ in range(RNG.randint(1, 8)):
        op = RNG.random()
        i = RNG.randrange(len(b)) if b else 0
        if op < 0.5 and b:
            b[i] ^= 1 << RNG.randrange(8)
        elif op < 0.7:
            b[i:i] = os.urandom(RNG.randint(1, 40))
        elif b:
            del b[i : i + RNG.randint(1, 40)]
    return bytes(b)


def test_ratchet_rejects_random_and_mutated_boxes_without_changing_state():
    alice, bob, _, _ = pair()
    good = alice.encrypt(b'{"t":"say","text":"hi"}')
    before = json.dumps(bob.to_dict(), sort_keys=True)
    for _ in range(400):
        bad = os.urandom(RNG.randint(0, 3000)) if RNG.random() < 0.3 else mutate(good)
        if bad == good:
            continue
        with pytest.raises(c.CryptoError):
            bob.decrypt(bad)
        assert json.dumps(bob.to_dict(), sort_keys=True) == before
    assert bob.decrypt(good) == b'{"t":"say","text":"hi"}'


def test_pairing_finish_rejects_garbage():
    identity, offer = c.ComputerIdentity.generate(), c.PairingOffer.new()
    keys = c.DeviceKeys.generate()
    msg, _, _ = c.device_finish(c.computer_bundle(identity, offer), identity.fingerprint(), offer.psk, keys)
    for _ in range(200):
        bad = dict(msg)
        k = RNG.choice(list(bad))
        bad[k] = RNG.choice(
            [c.b64e(os.urandom(RNG.randint(0, 1200))), "", "!!!", 5, None, c.b64e(mutate(c.b64d(bad[k])))]
        )
        with pytest.raises((c.CryptoError, TypeError, ValueError)):
            c.computer_finish(identity, offer, bad)


def test_frames_from_the_gateway_fail_validation_cleanly():
    samples = [
        '{"t":"e2e","dev":"x","box":"AAAA"}',
        '{"t":"pair","code":"ABC-DEF","body":{"t":"' + "x" * 5000 + '"}}',
        '{"t":"cert","id":"1","cert":{"v":1}}',
        '{"t":"hello","gateway":"' + "A" * 43 + '=","nonce":"AAAA","extra":1}',
        "[]",
        "null",
        "{",
    ]
    for _ in range(300):
        samples.append(mutate(RNG.choice(samples).encode()).decode("utf-8", "replace"))
    for raw in samples:
        with contextlib.suppress(ValidationError):
            TO_COMPUTER.validate_json(raw)


async def test_channel_survives_hostile_messages_from_a_paired_device(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    values = [None, 0, -1, 2**63, "x" * 30000, [], {}, {"$": 1}, "\x00\x1b[2J", 1.5, True]
    types = [
        "say",
        "approval_reply",
        "sync_request",
        "history_request",
        "hello",
        "revoke_device",
        "memories_request",
        "zzz",
    ]
    for _ in range(300):
        msg = {"t": RNG.choice(types)}
        for key in (
            "text",
            "cid",
            "id",
            "decision",
            "values",
            "note",
            "after",
            "before",
            "limit",
            "days",
            "name",
            "push",
            "device",
        ):
            if RNG.random() < 0.4:
                msg[key] = RNG.choice(values)
        await phone.send(msg)
        phone.inbox()
    acct = rig.accounts.find("Ana")
    assert phone.dev in acct.devices  # still paired, nothing crashed
    assert all(isinstance(t, str) and "\x00" not in t for t in rig.agent.said)
