# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""AHA-PQ v1: hybrid post-quantum pairing and a Double Ratchet with ML-KEM steps (SPEC §6.8).

Primitives come from pyca/cryptography (OpenSSL). This module only composes them, following the spec, and is
checked against the TypeScript implementation in aha-app with shared test vectors.

Byte strings everywhere; base64 only at the edges (`b64e`/`b64d`). The ratchet's JSON state format is shared
with aha-app.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac as _hmac
import json
import os
import struct
import time
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import mlkem, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 1
PROTOCOL = b"AHA-PQ-v1"
INFO_PAIR = b"AHA-PQ-v1 pair"
INFO_RATCHET = b"AHA-PQ-v1 ratchet"
INFO_MSG = b"AHA-PQ-v1 msg"
AD_PUSH = b"AHA-PQ-v1 push"

X25519_LEN = 32
KEM_PK_LEN = 1184
KEM_CT_LEN = 1088
KEM_SEED_LEN = 64
HEADER_LEN = 1 + 32 + 4 + 4 + KEM_CT_LEN
PAD_BLOCK = 256
MAX_BOX = 2 * 1024 * 1024
MAX_SKIP = 200  # per chain
MAX_SKIPPED_TOTAL = 1000
SKIPPED_TTL = 7 * 24 * 3600


class CryptoError(Exception):
    """Anything that doesn't verify or decrypt. Callers drop the message; no detail goes back to the sender."""


# ---- encoding


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode()


def b64d(text: str, length: int | None = None) -> bytes:
    try:
        data = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as e:
        raise CryptoError("bad base64") from e
    if length is not None and len(data) != length:
        raise CryptoError("bad length")
    return data


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_d(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as e:
        raise CryptoError("bad base64url") from e


# ---- primitives


def x25519_keypair(seed: bytes | None = None) -> tuple[bytes, bytes]:
    priv = x25519.X25519PrivateKey.from_private_bytes(seed) if seed else x25519.X25519PrivateKey.generate()
    return priv.private_bytes_raw(), priv.public_key().public_bytes_raw()


def dh(priv: bytes, pub: bytes) -> bytes:
    if len(pub) != X25519_LEN:
        raise CryptoError("bad X25519 key")
    try:
        out = x25519.X25519PrivateKey.from_private_bytes(priv).exchange(x25519.X25519PublicKey.from_public_bytes(pub))
    except ValueError as e:  # OpenSSL rejects an all-zero shared secret (low-order point)
        raise CryptoError("bad X25519 key") from e
    if not any(out):
        raise CryptoError("bad X25519 key")
    return out


def kem_keypair(seed: bytes | None = None) -> tuple[bytes, bytes]:
    seed = seed or os.urandom(KEM_SEED_LEN)
    key = mlkem.MLKEM768PrivateKey.from_seed_bytes(seed)
    return seed, key.public_key().public_bytes_raw()


def kem_encaps(pub: bytes) -> tuple[bytes, bytes]:
    if len(pub) != KEM_PK_LEN:
        raise CryptoError("bad ML-KEM key")
    try:
        ss, ct = mlkem.MLKEM768PublicKey.from_public_bytes(pub).encapsulate()
    except ValueError as e:
        raise CryptoError("bad ML-KEM key") from e
    return ss, ct


def kem_decaps(seed: bytes, ct: bytes) -> bytes:
    if len(ct) != KEM_CT_LEN:
        raise CryptoError("bad ML-KEM ciphertext")
    return mlkem.MLKEM768PrivateKey.from_seed_bytes(seed).decapsulate(ct)


def hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def hmac256(key: bytes, data: bytes) -> bytes:
    return _hmac.new(key, data, hashlib.sha256).digest()


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def seal(key: bytes, nonce: bytes, plaintext: bytes, ad: bytes) -> bytes:
    return ChaCha20Poly1305(key).encrypt(nonce, plaintext, ad)


def unseal(key: bytes, nonce: bytes, ciphertext: bytes, ad: bytes) -> bytes:
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, ad)
    except InvalidTag as e:
        raise CryptoError("does not decrypt") from e


def pad(data: bytes) -> bytes:
    """ISO/IEC 7816-4 padding to a multiple of PAD_BLOCK, so message lengths leak less."""
    data += b"\x80"
    return data + b"\x00" * (-len(data) % PAD_BLOCK)


def unpad(data: bytes) -> bytes:
    end = data.rstrip(b"\x00")
    if not end.endswith(b"\x80"):
        raise CryptoError("bad padding")
    return end[:-1]


# ---- pairing (SPEC §6.4, §6.8)


def fingerprint(ik_pub: bytes, kem_pub: bytes) -> bytes:
    return sha256(ik_pub + kem_pub)


def transcript(
    ikc: bytes, kemc: bytes, ekc: bytes, ikd: bytes, kemd: bytes, sigd: bytes, ekd: bytes, ct: bytes
) -> bytes:
    return sha256(INFO_PAIR + ikc + kemc + ekc + ikd + kemd + sigd + ekd + ct)


@dataclass(frozen=True)
class PairKeys:
    sk: bytes
    push_key: bytes
    confirm_d: bytes
    confirm_c: bytes
    th: bytes

    def device_mac(self) -> bytes:
        return hmac256(self.confirm_d, b"device" + self.th)

    def computer_mac(self) -> bytes:
        return hmac256(self.confirm_c, b"computer" + self.th)


def pair_keys(psk: bytes, th: bytes, dh1: bytes, dh2: bytes, dh3: bytes, dh4: bytes, ss: bytes) -> PairKeys:
    okm = hkdf(psk, b"\xff" * 32 + dh1 + dh2 + dh3 + dh4 + ss, INFO_PAIR + th, 128)
    return PairKeys(okm[:32], okm[32:64], okm[64:96], okm[96:128], th)


def session_ad(ikd: bytes, ikc: bytes) -> bytes:
    return PROTOCOL + ikd + ikc


@dataclass
class ComputerIdentity:
    """The computer's long-term end-to-end keys (data/keys/e2e.json)."""

    ik_priv: bytes
    ik_pub: bytes
    kem_seed: bytes
    kem_pub: bytes

    @classmethod
    def generate(cls) -> ComputerIdentity:
        ik_priv, ik_pub = x25519_keypair()
        kem_seed, kem_pub = kem_keypair()
        return cls(ik_priv, ik_pub, kem_seed, kem_pub)

    def fingerprint(self) -> bytes:
        return fingerprint(self.ik_pub, self.kem_pub)

    def to_dict(self) -> dict:
        return {"ik": b64e(self.ik_priv), "kem": b64e(self.kem_seed)}

    @classmethod
    def from_dict(cls, d: dict) -> ComputerIdentity:
        ik_priv, ik_pub = x25519_keypair(b64d(d["ik"], 32))
        kem_seed, kem_pub = kem_keypair(b64d(d["kem"], KEM_SEED_LEN))
        return cls(ik_priv, ik_pub, kem_seed, kem_pub)


@dataclass
class DeviceKeys:
    """A device's long-term keys (the app keeps these in IndexedDB; the Python test device in memory)."""

    ik_priv: bytes
    ik_pub: bytes
    kem_seed: bytes
    kem_pub: bytes
    sig_priv: bytes
    sig_pub: bytes

    @classmethod
    def generate(cls) -> DeviceKeys:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        ik_priv, ik_pub = x25519_keypair()
        kem_seed, kem_pub = kem_keypair()
        sig = ed25519.Ed25519PrivateKey.generate()
        return cls(ik_priv, ik_pub, kem_seed, kem_pub, sig.private_bytes_raw(), sig.public_key().public_bytes_raw())


@dataclass
class PairingOffer:
    """The computer's side of one pairing in progress: its fresh EKc and the QR secret."""

    psk: bytes
    ek_priv: bytes
    ek_pub: bytes

    @classmethod
    def new(cls) -> PairingOffer:
        ek_priv, ek_pub = x25519_keypair()
        return cls(os.urandom(32), ek_priv, ek_pub)


def computer_bundle(identity: ComputerIdentity, offer: PairingOffer) -> dict:
    return {"ikc": b64e(identity.ik_pub), "kemc": b64e(identity.kem_pub), "ekc": b64e(offer.ek_pub)}


def device_finish(bundle: dict, fp: bytes, psk: bytes, keys: DeviceKeys) -> tuple[dict, PairKeys, dict]:
    """Device step 4: check the bundle, derive the keys, build the finish message.

    Returns (message to send, pairing keys, values the device keeps: ekc and ad for the ratchet).
    """
    ikc, kemc, ekc = b64d(bundle["ikc"], 32), b64d(bundle["kemc"], KEM_PK_LEN), b64d(bundle["ekc"], 32)
    if not _hmac.compare_digest(fingerprint(ikc, kemc), fp):
        raise CryptoError("fingerprint mismatch")
    ek_priv, ek_pub = x25519_keypair()
    ss, ct = kem_encaps(kemc)
    th = transcript(ikc, kemc, ekc, keys.ik_pub, keys.kem_pub, keys.sig_pub, ek_pub, ct)
    pk = pair_keys(psk, th, dh(keys.ik_priv, ikc), dh(ek_priv, ikc), dh(keys.ik_priv, ekc), dh(ek_priv, ekc), ss)
    message = {
        "ikd": b64e(keys.ik_pub),
        "kemd": b64e(keys.kem_pub),
        "sigd": b64e(keys.sig_pub),
        "ekd": b64e(ek_pub),
        "ct": b64e(ct),
        "mac": b64e(pk.device_mac()),
    }
    return message, pk, {"ikc": ikc, "kemc": kemc, "ekc": ekc}


@dataclass(frozen=True)
class PairedDevice:
    ikd: bytes
    kemd: bytes
    sigd: bytes
    keys: PairKeys


def computer_finish(identity: ComputerIdentity, offer: PairingOffer, message: dict) -> PairedDevice:
    """Computer step 5: verify the device's finish message (constant-time MAC check)."""
    try:
        ikd, kemd = b64d(message["ikd"], 32), b64d(message["kemd"], KEM_PK_LEN)
        sigd, ekd = b64d(message["sigd"], 32), b64d(message["ekd"], 32)
        ct, mac = b64d(message["ct"], KEM_CT_LEN), b64d(message["mac"], 32)
    except (KeyError, TypeError) as e:
        raise CryptoError("bad finish message") from e
    ss = kem_decaps(identity.kem_seed, ct)
    th = transcript(identity.ik_pub, identity.kem_pub, offer.ek_pub, ikd, kemd, sigd, ekd, ct)
    pk = pair_keys(
        offer.psk,
        th,
        dh(identity.ik_priv, ikd),
        dh(identity.ik_priv, ekd),
        dh(offer.ek_priv, ikd),
        dh(offer.ek_priv, ekd),
        ss,
    )
    if not _hmac.compare_digest(pk.device_mac(), mac):
        raise CryptoError("pairing MAC mismatch")
    return PairedDevice(ikd, kemd, sigd, pk)


def device_check_computer(pk: PairKeys, mac_c: bytes) -> None:
    if not _hmac.compare_digest(pk.computer_mac(), mac_c):
        raise CryptoError("computer MAC mismatch")


# ---- Double Ratchet with ML-KEM steps (SPEC §6.8)


def kdf_rk(rk: bytes, dh_out: bytes, kem_ss: bytes) -> tuple[bytes, bytes]:
    out = hkdf(rk, dh_out + kem_ss, INFO_RATCHET, 64)
    return out[:32], out[32:]


def kdf_ck(ck: bytes) -> tuple[bytes, bytes]:
    return hmac256(ck, b"\x02"), hmac256(ck, b"\x01")  # (next chain key, message key)


def message_key_material(mk: bytes) -> tuple[bytes, bytes]:
    out = hkdf(b"\x00" * 32, mk, INFO_MSG, 44)
    return out[:32], out[32:]


@dataclass(frozen=True)
class Header:
    dh: bytes
    pn: int
    n: int
    kem_ct: bytes

    def encode(self) -> bytes:
        return bytes([VERSION]) + self.dh + struct.pack(">II", self.pn, self.n) + self.kem_ct

    @classmethod
    def decode(cls, data: bytes) -> Header:
        if len(data) < HEADER_LEN or data[0] != VERSION:
            raise CryptoError("bad header")
        pn, n = struct.unpack(">II", data[33:41])
        return cls(data[1:33], pn, n, data[41:HEADER_LEN])


@dataclass
class Ratchet:
    ad: bytes
    rk: bytes
    dhs_priv: bytes
    dhs_pub: bytes
    dhr: bytes | None
    cks: bytes | None
    ckr: bytes | None
    ns: int
    nr: int
    pn: int
    my_kem_seed: bytes
    peer_kem_pub: bytes
    send_kem_ct: bytes | None
    skipped: dict[str, list] = field(default_factory=dict)  # "<b64 dh>:<n>" → [b64 mk, unix time]

    # -- setup

    @classmethod
    def init_device(cls, sk: bytes, ad: bytes, ekc: bytes, kem_seed: bytes, kemc: bytes) -> Ratchet:
        """'Alice': ratchets at once towards the computer's pairing key EKc."""
        dhs_priv, dhs_pub = x25519_keypair()
        ss, ct = kem_encaps(kemc)
        rk, cks = kdf_rk(sk, dh(dhs_priv, ekc), ss)
        return cls(ad, rk, dhs_priv, dhs_pub, ekc, cks, None, 0, 0, 0, kem_seed, kemc, ct)

    @classmethod
    def init_computer(
        cls, sk: bytes, ad: bytes, ek_priv: bytes, ek_pub: bytes, kem_seed: bytes, kemd: bytes
    ) -> Ratchet:
        """'Bob': waits for the device's first message; its first ratchet key is EKc."""
        return cls(ad, sk, ek_priv, ek_pub, None, None, None, 0, 0, 0, kem_seed, kemd, None)

    # -- serialisation (format shared with aha-app)

    def to_dict(self) -> dict:
        def opt(b):
            return b64e(b) if b is not None else None

        return {
            "v": VERSION,
            "ad": b64e(self.ad),
            "rk": b64e(self.rk),
            "dhs": [b64e(self.dhs_priv), b64e(self.dhs_pub)],
            "dhr": opt(self.dhr),
            "cks": opt(self.cks),
            "ckr": opt(self.ckr),
            "ns": self.ns,
            "nr": self.nr,
            "pn": self.pn,
            "kem": b64e(self.my_kem_seed),
            "peer_kem": b64e(self.peer_kem_pub),
            "send_ct": opt(self.send_kem_ct),
            "skipped": self.skipped,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Ratchet:
        def opt(v, n=None):
            return b64d(v, n) if v is not None else None

        if d.get("v") != VERSION:
            raise CryptoError("unknown state version")
        return cls(
            b64d(d["ad"]),
            b64d(d["rk"], 32),
            b64d(d["dhs"][0], 32),
            b64d(d["dhs"][1], 32),
            opt(d["dhr"], 32),
            opt(d["cks"], 32),
            opt(d["ckr"], 32),
            int(d["ns"]),
            int(d["nr"]),
            int(d["pn"]),
            b64d(d["kem"], KEM_SEED_LEN),
            b64d(d["peer_kem"], KEM_PK_LEN),
            opt(d["send_ct"], KEM_CT_LEN),
            dict(d.get("skipped", {})),
        )

    # -- messages

    def encrypt(self, plaintext: bytes) -> bytes:
        if self.cks is None or self.send_kem_ct is None:
            raise CryptoError("no sending chain yet")  # the computer can't send before the device's first message
        self.cks, mk = kdf_ck(self.cks)
        header = Header(self.dhs_pub, self.pn, self.ns, self.send_kem_ct).encode()
        self.ns += 1
        key, nonce = message_key_material(mk)
        return header + seal(key, nonce, pad(plaintext), self.ad + header)

    def decrypt(self, box: bytes, now: float | None = None) -> bytes:
        """All or nothing: the state changes only if the message decrypts."""
        if len(box) > MAX_BOX:
            raise CryptoError("too large")
        work = copy.deepcopy(self)
        plaintext = work._decrypt(box, time.time() if now is None else now)
        self.__dict__.update(work.__dict__)
        return plaintext

    def _decrypt(self, box: bytes, now: float) -> bytes:
        self._expire_skipped(now)
        header_bytes, body = box[:HEADER_LEN], box[HEADER_LEN:]
        header = Header.decode(header_bytes)
        slot = f"{b64e(header.dh)}:{header.n}"
        if slot in self.skipped:
            mk = b64d(self.skipped.pop(slot)[0], 32)
            return self._open(mk, header_bytes, body)
        if header.dh != self.dhr:
            self._skip(header.pn, now)
            self._dh_ratchet(header)
        self._skip(header.n, now)
        self.ckr, mk = kdf_ck(self.ckr)
        self.nr += 1
        return self._open(mk, header_bytes, body)

    def _open(self, mk: bytes, header_bytes: bytes, body: bytes) -> bytes:
        key, nonce = message_key_material(mk)
        return unpad(unseal(key, nonce, body, self.ad + header_bytes))

    def _skip(self, until: int, now: float) -> None:
        if self.ckr is None:
            return
        if until - self.nr > MAX_SKIP:
            raise CryptoError("too far ahead")
        while self.nr < until:
            self.ckr, mk = kdf_ck(self.ckr)
            self.skipped[f"{b64e(self.dhr)}:{self.nr}"] = [b64e(mk), int(now)]
            self.nr += 1
        while len(self.skipped) > MAX_SKIPPED_TOTAL:  # drop the oldest
            del self.skipped[min(self.skipped, key=lambda k: self.skipped[k][1])]

    def _expire_skipped(self, now: float) -> None:
        self.skipped = {k: v for k, v in self.skipped.items() if now - v[1] < SKIPPED_TTL}

    def _dh_ratchet(self, header: Header) -> None:
        self.pn, self.ns, self.nr = self.ns, 0, 0
        self.dhr = header.dh
        ss = kem_decaps(self.my_kem_seed, header.kem_ct)
        self.rk, self.ckr = kdf_rk(self.rk, dh(self.dhs_priv, self.dhr), ss)
        self.dhs_priv, self.dhs_pub = x25519_keypair()
        ss2, self.send_kem_ct = kem_encaps(self.peer_kem_pub)
        self.rk, self.cks = kdf_rk(self.rk, dh(self.dhs_priv, self.dhr), ss2)


def encrypt_json(ratchet: Ratchet, message: dict) -> str:
    return b64e(ratchet.encrypt(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()))


def decrypt_json(ratchet: Ratchet, box_b64: str) -> dict:
    box = b64d(box_b64)
    plaintext = ratchet.decrypt(box)
    try:
        message = json.loads(plaintext)
    except (ValueError, UnicodeDecodeError) as e:
        raise CryptoError("not JSON") from e
    if not isinstance(message, dict) or not isinstance(message.get("t"), str):
        raise CryptoError("not a message")
    return message


# ---- push payloads (SPEC §6.6)


def push_seal(push_key: bytes, payload: dict, nonce: bytes | None = None) -> dict:
    nonce = nonce or os.urandom(12)
    body = json.dumps(payload, separators=(",", ":")).encode()
    return {"v": VERSION, "n": b64e(nonce), "box": b64e(seal(push_key, nonce, body, AD_PUSH))}


def push_open(push_key: bytes, sealed: dict) -> dict:
    if sealed.get("v") != VERSION:
        raise CryptoError("bad push version")
    return json.loads(unseal(push_key, b64d(sealed["n"], 12), b64d(sealed["box"]), AD_PUSH))
