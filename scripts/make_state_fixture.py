# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Write tests/fixtures/state-v1.json: today's stored formats, frozen (SPEC §9.2).

Run once per format version. Never regenerate it to make a failing test pass: a failure means a stored format
changed, and the change needs migration code that reads the old one.
"""

import json
from pathlib import Path

from aha_local.e2e import crypto as c

identity, offer, device = c.ComputerIdentity.generate(), c.PairingOffer.new(), c.DeviceKeys.generate()
msg, dpk, kept = c.device_finish(c.computer_bundle(identity, offer), identity.fingerprint(), offer.psk, device)
paired = c.computer_finish(identity, offer, msg)
ad = c.session_ad(device.ik_pub, identity.ik_pub)
alice = c.Ratchet.init_device(dpk.sk, ad, kept["ekc"], device.kem_seed, kept["kemc"])
bob = c.Ratchet.init_computer(paired.keys.sk, ad, offer.ek_priv, offer.ek_pub, identity.kem_seed, paired.kemd)
for i in range(2):
    bob.decrypt(alice.encrypt(f"hi {i}".encode()))
    alice.decrypt(bob.encrypt(f"reply {i}".encode()))
state = {"computer_ratchet": bob.to_dict(), "device_ratchet": alice.to_dict()}
from_device = alice.encrypt(b"after the snapshot")
state["box_from_device"] = c.b64e(from_device)
state["identity"] = identity.to_dict()
state["fingerprint"] = c.b64e(identity.fingerprint())
out = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "state-v1.json"
out.write_text(json.dumps(state, indent=1, sort_keys=True))
print(f"wrote {out}")
