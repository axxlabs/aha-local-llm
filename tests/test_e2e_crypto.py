# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""AHA-PQ v1 (SPEC §6.8): pairing, ratchet, push, and the failure cases that matter."""

import json
import os
from pathlib import Path

import pytest

from aha_local.e2e import crypto as c


def pair():
    """A full pairing; returns (device ratchet, computer ratchet, device pair keys, paired-device record)."""
    identity = c.ComputerIdentity.generate()
    offer = c.PairingOffer.new()
    device = c.DeviceKeys.generate()
    bundle = c.computer_bundle(identity, offer)
    msg, dpk, kept = c.device_finish(bundle, identity.fingerprint(), offer.psk, device)
    paired = c.computer_finish(identity, offer, msg)
    c.device_check_computer(dpk, paired.keys.computer_mac())
    assert dpk == paired.keys
    ad = c.session_ad(device.ik_pub, identity.ik_pub)
    alice = c.Ratchet.init_device(dpk.sk, ad, kept["ekc"], device.kem_seed, kept["kemc"])
    bob = c.Ratchet.init_computer(paired.keys.sk, ad, offer.ek_priv, offer.ek_pub, identity.kem_seed, paired.kemd)
    return alice, bob, dpk, paired


def test_pairing_and_conversation_both_ways():
    alice, bob, _, _ = pair()
    with pytest.raises(c.CryptoError):
        bob.encrypt(b"too early")  # the computer speaks only after the device's first message
    for i in range(3):
        assert bob.decrypt(alice.encrypt(f"hi {i}".encode())) == f"hi {i}".encode()
        assert alice.decrypt(bob.encrypt(f"reply {i}".encode())) == f"reply {i}".encode()


def test_wrong_psk_fails():
    identity, offer, device = c.ComputerIdentity.generate(), c.PairingOffer.new(), c.DeviceKeys.generate()
    msg, _, _ = c.device_finish(c.computer_bundle(identity, offer), identity.fingerprint(), os.urandom(32), device)
    with pytest.raises(c.CryptoError):
        c.computer_finish(identity, offer, msg)


def test_substituted_bundle_is_refused_by_fingerprint():
    identity, offer, device = c.ComputerIdentity.generate(), c.PairingOffer.new(), c.DeviceKeys.generate()
    attacker = c.ComputerIdentity.generate()
    with pytest.raises(c.CryptoError):
        c.device_finish(c.computer_bundle(attacker, offer), identity.fingerprint(), offer.psk, device)


def test_man_in_the_middle_swapping_the_ephemeral_key_fails():
    identity, offer, device = c.ComputerIdentity.generate(), c.PairingOffer.new(), c.DeviceKeys.generate()
    bundle = c.computer_bundle(identity, offer)
    evil = c.PairingOffer.new()
    bundle["ekc"] = c.b64e(evil.ek_pub)  # the relay swaps EKc; it doesn't know psk
    msg, _, _ = c.device_finish(bundle, identity.fingerprint(), offer.psk, device)
    with pytest.raises(c.CryptoError):
        c.computer_finish(identity, offer, msg)


def test_tampered_finish_message_fails():
    identity, offer, device = c.ComputerIdentity.generate(), c.PairingOffer.new(), c.DeviceKeys.generate()
    msg, _, _ = c.device_finish(c.computer_bundle(identity, offer), identity.fingerprint(), offer.psk, device)
    other = c.DeviceKeys.generate()
    msg["sigd"] = c.b64e(other.sig_pub)  # swap the device's gateway key: the transcript changes
    with pytest.raises(c.CryptoError):
        c.computer_finish(identity, offer, msg)


def test_wrong_computer_mac_fails():
    _, _, dpk, _ = pair()
    with pytest.raises(c.CryptoError):
        c.device_check_computer(dpk, os.urandom(32))


def test_out_of_order_and_replay():
    alice, bob, _, _ = pair()
    bob.decrypt(alice.encrypt(b"first"))
    boxes = [bob.encrypt(f"m{i}".encode()) for i in range(5)]
    assert alice.decrypt(boxes[3]) == b"m3"
    assert alice.decrypt(boxes[0]) == b"m0"
    assert alice.decrypt(boxes[4]) == b"m4"
    with pytest.raises(c.CryptoError):
        alice.decrypt(boxes[3])  # replay: the key was used and deleted
    assert alice.decrypt(boxes[1]) == b"m1"
    assert alice.decrypt(boxes[2]) == b"m2"


def test_messages_from_an_old_chain_after_a_ratchet_step():
    alice, bob, _, _ = pair()
    bob.decrypt(alice.encrypt(b"a"))
    late = bob.encrypt(b"late")
    bob_now = bob.encrypt(b"now")
    alice.decrypt(bob_now)
    bob.decrypt(alice.encrypt(b"step"))  # new ratchet step both ways
    newer = bob.encrypt(b"newer")
    assert alice.decrypt(newer) == b"newer"
    assert alice.decrypt(late) == b"late"


def test_tampering_is_detected_and_changes_nothing():
    alice, bob, _, _ = pair()
    box = bytearray(alice.encrypt(b"hello"))
    before = json.dumps(bob.to_dict(), sort_keys=True)
    for i in (5, 50, len(box) - 3):  # header dh, header kem ct, body tag
        bad = bytearray(box)
        bad[i] ^= 1
        with pytest.raises(c.CryptoError):
            bob.decrypt(bytes(bad))
        assert json.dumps(bob.to_dict(), sort_keys=True) == before
    assert bob.decrypt(bytes(box)) == b"hello"


def test_too_far_ahead_is_rejected():
    alice, bob, _, _ = pair()
    bob.decrypt(alice.encrypt(b"x"))
    for _ in range(c.MAX_SKIP + 1):
        bob.encrypt(b"lost")
    with pytest.raises(c.CryptoError):
        alice.decrypt(bob.encrypt(b"far"))


def test_state_round_trips_through_json():
    alice, bob, _, _ = pair()
    bob.decrypt(alice.encrypt(b"x"))
    skipped_box = bob.encrypt(b"skipped")
    alice.decrypt(bob.encrypt(b"y"))
    alice2 = c.Ratchet.from_dict(json.loads(json.dumps(alice.to_dict())))
    assert alice2.decrypt(skipped_box) == b"skipped"
    bob2 = c.Ratchet.from_dict(json.loads(json.dumps(bob.to_dict())))
    assert bob2.decrypt(alice2.encrypt(b"z")) == b"z"


def test_lengths_are_padded():
    alice, _, _, _ = pair()
    sizes = {len(alice.encrypt(b"a" * n)) for n in (1, 50, 200)}
    assert len(sizes) == 1


def test_forward_secrecy_keys_change_every_message():
    alice, bob, _, _ = pair()
    bob.decrypt(alice.encrypt(b"x"))
    keys = set()
    for _ in range(3):
        keys.add(bob.cks)
        alice.decrypt(bob.encrypt(b"y"))
    assert len(keys) == 3


def test_json_helpers_reject_non_messages():
    alice, bob, _, _ = pair()
    bob.decrypt(alice.encrypt(b"warm up"))
    box = c.b64e(alice.encrypt(b"[1,2]"))
    with pytest.raises(c.CryptoError):
        c.decrypt_json(bob, box)
    assert c.decrypt_json(bob, c.encrypt_json(alice, {"t": "say", "text": "hé"})) == {"t": "say", "text": "hé"}


def test_push_payloads():
    _, _, dpk, _ = pair()
    sealed = c.push_seal(dpk.push_key, {"seq": 7})
    assert c.push_open(dpk.push_key, sealed) == {"seq": 7}
    with pytest.raises(c.CryptoError):
        c.push_open(os.urandom(32), sealed)


def test_low_order_x25519_point_is_rejected():
    priv, _ = c.x25519_keypair()
    with pytest.raises(c.CryptoError):
        c.dh(priv, bytes(32))


# ---- shared vectors (SPEC §6.8)

VECTORS = __import__("pathlib").Path(__file__).parent / "vectors"


def check_vectors(v: dict) -> None:
    p = v["pairing"]
    s = {k: c.b64d(x) for k, x in p["seeds"].items()}
    _ikc_p, ikc = c.x25519_keypair(s["ikc"])
    kemc_s, kemc = c.kem_keypair(s["kemc"])
    _ekc_p, ekc = c.x25519_keypair(s["ekc"])
    ikd_p, ikd = c.x25519_keypair(s["ikd"])
    _, kemd = c.kem_keypair(s["kemd"])
    from cryptography.hazmat.primitives.asymmetric import ed25519

    sigd = ed25519.Ed25519PrivateKey.from_private_bytes(s["sigd"]).public_key().public_bytes_raw()
    ekd_p, ekd = c.x25519_keypair(s["ekd"])
    ct = c.b64d(p["kem_ct"])
    ss = c.kem_decaps(kemc_s, ct)
    th = c.transcript(ikc, kemc, ekc, ikd, kemd, sigd, ekd, ct)
    pk = c.pair_keys(c.b64d(p["psk"]), th, c.dh(ikd_p, ikc), c.dh(ekd_p, ikc), c.dh(ikd_p, ekc), c.dh(ekd_p, ekc), ss)
    e = {k: c.b64d(x) for k, x in p["expect"].items()}
    assert c.fingerprint(ikc, kemc) == e["fp"] and th == e["th"]
    assert (pk.sk, pk.push_key, pk.confirm_d, pk.confirm_c) == (e["sk"], e["push_key"], e["confirm_d"], e["confirm_c"])
    assert pk.device_mac() == e["mac"] and pk.computer_mac() == e["mac_c"]
    assert c.session_ad(ikd, ikc) == e["ad"]
    for step in v["ratchet"]:
        r = c.Ratchet.from_dict(step["state"])
        assert r.decrypt(c.b64d(step["box"]), now=step["now"]) == c.b64d(step["plaintext"])
    assert c.push_open(c.b64d(v["push"]["key"]), v["push"]["sealed"]) == v["push"]["payload"]


def test_our_vectors_verify():
    check_vectors(json.loads((VECTORS / "aha_pq_v1.json").read_text()))


def test_vectors_written_by_the_app_verify():
    """aha-app writes vectors from its own TypeScript implementation; they must decrypt here too."""
    path = __import__("pathlib").Path(__file__).resolve().parents[2] / "aha-app" / "test" / "vectors" / "from_ts.json"
    if not path.exists():
        pytest.skip("aha-app not checked out next to aha-local-llm")
    check_vectors(json.loads(path.read_text()))


def test_stored_formats_from_v1_still_load():
    """Frozen state from the first release (SPEC §9.2): a format change must ship with migration code."""
    fx = json.loads((Path(__file__).parent / "fixtures" / "state-v1.json").read_text())
    bob = c.Ratchet.from_dict(fx["computer_ratchet"])
    assert bob.decrypt(c.b64d(fx["box_from_device"])) == b"after the snapshot"
    alice = c.Ratchet.from_dict(fx["device_ratchet"])
    assert alice.decrypt(bob.encrypt(b"and back")) == b"and back"
    identity = c.ComputerIdentity.from_dict(fx["identity"])
    assert c.b64e(identity.fingerprint()) == fx["fingerprint"]
