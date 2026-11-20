# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Write AHA-PQ v1 test vectors (SPEC §6.8) for aha-app: `uv run python scripts/make_vectors.py`.

Pairing outputs are deterministic (fixed seeds); ratchet vectors record the receiver's state before each box
and the expected plaintext, so another implementation can check decryption across ratchet steps, KEM steps,
skipped keys and out-of-order delivery.
"""

import hashlib
import json
import sys
from pathlib import Path

from aha_local.e2e import crypto as c

OUT = Path(__file__).resolve().parents[1] / "tests" / "vectors" / "aha_pq_v1.json"


def seed(label: str, n: int = 32) -> bytes:
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha256(f"aha-vector|{label}|{i}".encode()).digest()
        i += 1
    return out[:n]


def pairing() -> dict:
    _ikc_p, ikc = c.x25519_keypair(seed("ikc"))
    kemc_s, kemc = c.kem_keypair(seed("kemc", 64))
    _ekc_p, ekc = c.x25519_keypair(seed("ekc"))
    ikd_p, ikd = c.x25519_keypair(seed("ikd"))
    _, kemd = c.kem_keypair(seed("kemd", 64))
    from cryptography.hazmat.primitives.asymmetric import ed25519

    sigd = ed25519.Ed25519PrivateKey.from_private_bytes(seed("sigd")).public_key().public_bytes_raw()
    ekd_p, ekd = c.x25519_keypair(seed("ekd"))
    psk = seed("psk")
    ss, ct = c.kem_encaps(kemc)
    assert c.kem_decaps(kemc_s, ct) == ss
    th = c.transcript(ikc, kemc, ekc, ikd, kemd, sigd, ekd, ct)
    pk = c.pair_keys(psk, th, c.dh(ikd_p, ikc), c.dh(ekd_p, ikc), c.dh(ikd_p, ekc), c.dh(ekd_p, ekc), ss)
    b = c.b64e
    return {
        "seeds": {
            k: b(seed(k, 64 if k.startswith("kem") else 32))
            for k in ("ikc", "kemc", "ekc", "ikd", "kemd", "sigd", "ekd")
        },
        "psk": b(psk),
        "kem_ct": b(ct),
        "expect": {
            "fp": b(c.fingerprint(ikc, kemc)),
            "th": b(th),
            "sk": b(pk.sk),
            "push_key": b(pk.push_key),
            "confirm_d": b(pk.confirm_d),
            "confirm_c": b(pk.confirm_c),
            "mac": b(pk.device_mac()),
            "mac_c": b(pk.computer_mac()),
            "ad": b(c.session_ad(ikd, ikc)),
        },
    }


def ratchet() -> list[dict]:
    identity = c.ComputerIdentity.generate()
    offer = c.PairingOffer.new()
    device = c.DeviceKeys.generate()
    msg, dpk, kept = c.device_finish(c.computer_bundle(identity, offer), identity.fingerprint(), offer.psk, device)
    paired = c.computer_finish(identity, offer, msg)
    ad = c.session_ad(device.ik_pub, identity.ik_pub)
    sides = {
        "device": c.Ratchet.init_device(dpk.sk, ad, kept["ekc"], device.kem_seed, kept["kemc"]),
        "computer": c.Ratchet.init_computer(
            paired.keys.sk, ad, offer.ek_priv, offer.ek_pub, identity.kem_seed, paired.kemd
        ),
    }
    now = 1_800_000_000
    steps = []

    def send(frm: str, text: str) -> tuple[str, bytes, bytes]:
        pt = json.dumps({"t": "msg", "text": text}, ensure_ascii=False).encode()
        return frm, sides[frm].encrypt(pt), pt

    def deliver(item):
        frm, box, pt = item
        to = "computer" if frm == "device" else "device"
        steps.append({"to": to, "state": sides[to].to_dict(), "box": c.b64e(box), "plaintext": c.b64e(pt), "now": now})
        assert sides[to].decrypt(box, now=now) == pt

    deliver(send("device", "hello"))
    batch = [send("computer", f"reply {i} ✅") for i in range(4)]
    for i in (2, 0, 3, 1):  # out of order
        deliver(batch[i])
    deliver(send("device", "second turn"))
    late = send("device", "late one")
    deliver(send("device", "third"))
    deliver(send("computer", "x" * 600))  # crosses padding blocks
    deliver(late)
    return steps


def push() -> dict:
    key, nonce = seed("push"), seed("nonce", 12)
    sealed = c.push_seal(key, {"seq": 42, "preview": "Booked ✅"}, nonce=nonce)
    return {"key": c.b64e(key), "payload": {"seq": 42, "preview": "Booked ✅"}, "sealed": sealed}


def main() -> None:
    vectors = {"version": "AHA-PQ-v1", "pairing": pairing(), "ratchet": ratchet(), "push": push()}
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(vectors['ratchet'])} ratchet steps)", file=sys.stderr)


if __name__ == "__main__":
    main()
