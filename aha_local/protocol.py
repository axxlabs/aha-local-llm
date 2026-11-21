# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Frames between the gateway, aha computers and devices (SPEC §6.2, §6.3, §6.5).

This file is identical in aha-gateway and aha-local-llm; a test in each repo compares them.
Everything the gateway handles is here: it never sees message content, only these envelopes and opaque boxes,
apart from a problem report the person reviewed and chose to send (`Report`, SPEC §7.7).
"""

from __future__ import annotations

import base64
import json
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL_VERSION = 2
HANDSHAKE_CONTEXT = "aha-tunnel-v1"
DEVICE_CONTEXT = "aha-device-v1"
GATEWAY_PROOF_CONTEXT = "aha-gateway-v1"  # the gateway signs a device's nonce (SPEC §6.3)
# What each side supports (SPEC §6.5). Optional frames are only sent to a side that lists them.
GATEWAY_FEATURES = ("report", "cert_code", "ready_sig")
COMPUTER_FEATURES: tuple[str, ...] = ()
MAX_BOX_B64 = 2_800_000  # a 2 MB ratchet message in base64
MAX_FRAME_BYTES = 3_000_000
MAX_PAIR_FIELD = 4_000

B64_PATTERN = r"^[A-Za-z0-9+/]+={0,2}$"
KEY_PATTERN = r"^[A-Za-z0-9+/]{43}=$"  # 32 bytes
TAG_PATTERN = r"^[A-Za-z0-9+/]{22}==$"  # 16 bytes
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
CODE_PATTERN = r"^[A-Z2-9]{3}-[A-Z2-9]{3}$"
FEATURE_PATTERN = r"^[a-z_]{1,24}$"

B64 = Annotated[str, Field(pattern=B64_PATTERN, max_length=MAX_BOX_B64)]
Key = Annotated[str, Field(pattern=KEY_PATTERN)]
Tag = Annotated[str, Field(pattern=TAG_PATTERN)]
FrameId = Annotated[str, Field(pattern=ID_PATTERN)]
Code = Annotated[str, Field(pattern=CODE_PATTERN)]
Features = Annotated[list[Annotated[str, Field(pattern=FEATURE_PATTERN)]], Field(max_length=32)]
Nonce = Annotated[str, Field(pattern=B64_PATTERN, max_length=64)]
PairBody = Annotated[
    dict[Annotated[str, Field(pattern=r"^[a-z_]{1,16}$")], Annotated[str, Field(max_length=MAX_PAIR_FIELD)]],
    Field(max_length=12),
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Cert(Strict):
    """A binding certificate: routes a device's frames to an instance (SPEC §6.4). Never grants trust."""

    v: Literal[2] = 2
    device: Key  # the device's Ed25519 auth key (SIGd)
    instance: Key
    tag: Tag  # random per-account routing tag (billing counts tags, learns nothing else)
    issued: int
    expires: int
    sig: B64 = ""


# ---- handshakes


class Hello(Strict):
    """gateway → computer or device, first frame."""

    t: Literal["hello"] = "hello"
    v: int = PROTOCOL_VERSION
    gateway: Key
    nonce: B64
    vapid: Annotated[str, Field(max_length=200)] = ""  # Web Push application server key (base64url), for devices
    features: Features = []


class Auth(Strict):
    t: Literal["auth"] = "auth"
    instance: Key
    nonce: B64
    sig: B64
    certs: Annotated[list[Cert], Field(max_length=1024)] = []
    name: Annotated[str, Field(max_length=60)] = ""
    version: Annotated[str, Field(max_length=40)] = ""
    features: Features = []


class Ready(Strict):
    """gateway → computer: handshake done; which device certificates it accepted."""

    t: Literal["ready"] = "ready"
    sig: B64
    routes: Annotated[list[Key], Field(max_length=1024)] = []
    rejected: Annotated[list[Key], Field(max_length=1024)] = []


class DevAuth(Strict):
    t: Literal["dev_auth"] = "dev_auth"
    cert: Cert
    sig: B64
    nonce: Nonce | None = None  # the gateway signs it in `ready` to prove it holds the pinned key


class DeviceReady(Strict):
    """gateway → device: authenticated; whether its computer is connected right now."""

    t: Literal["ready"] = "ready"
    online: bool
    sig: B64 | None = None  # over GATEWAY_PROOF_CONTEXT|<device nonce>


# ---- after the handshake


class E2E(Strict):
    """An opaque ratchet message between a device and its computer, on the tunnel."""

    t: Literal["e2e"] = "e2e"
    dev: Key
    box: B64


class DevE2E(Strict):
    """The same, on the device's own connection (the gateway adds or strips `dev`)."""

    t: Literal["e2e"] = "e2e"
    box: B64


class PairStart(Strict):
    t: Literal["pair_start"] = "pair_start"


class PairCode(Strict):
    t: Literal["pair_code"] = "pair_code"
    code: Code
    expires_in: int


class PairHello(Strict):
    """device → gateway: join the pairing with this rendezvous code instead of authenticating."""

    t: Literal["pair_hello"] = "pair_hello"
    code: Code
    nonce: Nonce | None = None


class Pair(Strict):
    """A pairing handshake message, on the tunnel (matched by code)."""

    t: Literal["pair"] = "pair"
    code: Code
    body: PairBody


class DevPair(Strict):
    """The same, on the pairing device's connection."""

    t: Literal["pair"] = "pair"
    body: PairBody


class CertRequest(Strict):
    t: Literal["cert_request"] = "cert_request"
    id: FrameId
    device: Key
    tag: Tag
    code: Code | None = None  # the rendezvous code the device joined (new devices; SPEC §6.4)


class CertIssued(Strict):
    t: Literal["cert"] = "cert"
    id: FrameId
    cert: Cert


class Push(Strict):
    """computer → gateway: deliver an RFC 8291-encrypted push; the gateway adds VAPID and POSTs it."""

    t: Literal["push"] = "push"
    endpoint: Annotated[str, Field(pattern=r"^https://[A-Za-z0-9.-]+(:\d+)?/\S*$", max_length=1024)]
    body: Annotated[str, Field(pattern=B64_PATTERN, max_length=8_000)]
    ttl: Annotated[int, Field(ge=0, le=2_419_200)] = 86_400
    urgency: Literal["very-low", "low", "normal", "high"] = "normal"


class Revoke(Strict):
    t: Literal["revoke"] = "revoke"
    dev: Key


class DeviceStatus(Strict):
    """gateway → computer: a device connected or went away (so the computer knows when to push)."""

    t: Literal["device"] = "device"
    dev: Key
    online: bool


class ComputerStatus(Strict):
    """gateway → device: its computer connected or went away."""

    t: Literal["computer"] = "computer"
    online: bool


class Notice(Strict):
    t: Literal["notice"] = "notice"
    code: Annotated[str, Field(pattern=r"^[a-z_]{1,40}$")]
    detail: Annotated[str, Field(max_length=200)] = ""


class Report(Strict):
    """computer → gateway: a problem report the person previewed and chose to send (SPEC §7.7)."""

    t: Literal["report"] = "report"
    id: FrameId
    title: Annotated[str, Field(min_length=1, max_length=120)]
    body: Annotated[str, Field(min_length=1, max_length=60_000)]


class ReportResult(Strict):
    t: Literal["report_result"] = "report_result"
    id: FrameId
    ok: bool
    code: Annotated[str, Field(pattern=r"^[a-z_]{0,40}$")] = ""


class Ping(Strict):
    t: Literal["ping"] = "ping"


class Pong(Strict):
    t: Literal["pong"] = "pong"


ToComputer = Annotated[
    Hello | Ready | E2E | PairCode | Pair | CertIssued | DeviceStatus | ReportResult | Notice | Ping | Pong,
    Field(discriminator="t"),
]
FromComputer = Annotated[
    Auth | E2E | PairStart | Pair | CertRequest | Push | Revoke | Report | Ping | Pong, Field(discriminator="t")
]
ToDevice = Annotated[
    Hello | DeviceReady | DevE2E | DevPair | ComputerStatus | Notice | Ping | Pong, Field(discriminator="t")
]
FromDevice = Annotated[DevAuth | PairHello | DevE2E | DevPair | Ping | Pong, Field(discriminator="t")]


class Frames:
    """A parser for one direction. Known frame types are validated strictly; a frame type this version doesn't
    know returns None, to be ignored: never a reason to close the connection (SPEC §6.5)."""

    def __init__(self, union: Any) -> None:
        self.adapter = TypeAdapter(union)
        members = get_args(get_args(union)[0])
        self.tags = frozenset(m.model_fields["t"].default for m in members)
        self.unknown = 0

    def validate_json(self, raw: str | bytes):
        try:
            tag = json.loads(raw).get("t")
        except (ValueError, AttributeError):
            tag = None
        if isinstance(tag, str) and tag not in self.tags:
            self.unknown += 1
            return None
        return self.adapter.validate_json(raw)


TO_COMPUTER = Frames(ToComputer)
FROM_COMPUTER = Frames(FromComputer)
TO_DEVICE = Frames(ToDevice)
FROM_DEVICE = Frames(FromDevice)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)
