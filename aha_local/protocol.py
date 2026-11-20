# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Tunnel frames between the gateway and an aha computer (SPEC §6).

This file is identical in aha-gateway and aha-local-llm; tests in both repos check it against the shared
fixtures, so the two stay in step without depending on each other.
"""

from __future__ import annotations

import base64
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL_VERSION = 1
HANDSHAKE_CONTEXT = "aha-tunnel-v1"
MAX_FRAME_BYTES = 1_000_000
MAX_IMAGE_FRAME_BYTES = 2_000_000
MAX_TEXT_CHARS = 4096
MAX_BUTTONS = 3
MAX_BUTTON_LABEL = 20

# An address is "<channel>:<id>": sim:+447900000001, wa:+447700900123, tg:123456789.
ADDRESS_PATTERN = r"^(sim|wa):\+[1-9]\d{6,14}$|^tg:\d{1,20}$"
B64_PATTERN = r"^[A-Za-z0-9+/]+={0,2}$"
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

Address = Annotated[str, Field(pattern=ADDRESS_PATTERN)]
B64 = Annotated[str, Field(pattern=B64_PATTERN, max_length=2_800_000)]
FrameId = Annotated[str, Field(pattern=ID_PATTERN)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Button(Strict):
    id: Annotated[str, Field(pattern=ID_PATTERN)]
    label: Annotated[str, Field(min_length=1, max_length=MAX_BUTTON_LABEL)]


class Image(Strict):
    mime: Literal["image/png", "image/jpeg", "image/webp"]
    b64: B64


class Cert(Strict):
    """A binding certificate: the gateway's signature that `address` belongs to `instance` (SPEC §6.4)."""

    v: Literal[1] = 1
    address: Address
    instance: B64
    issued: int
    expires: int
    sig: B64 = ""


# ---- handshake


class Hello(Strict):
    t: Literal["hello"] = "hello"
    v: int = PROTOCOL_VERSION
    gateway: B64
    nonce: B64


class Auth(Strict):
    t: Literal["auth"] = "auth"
    instance: B64
    nonce: B64
    sig: B64
    certs: Annotated[list[Cert], Field(max_length=64)] = []
    name: Annotated[str, Field(max_length=60)] = ""
    version: Annotated[str, Field(max_length=40)] = ""


class Ready(Strict):
    t: Literal["ready"] = "ready"
    sig: B64
    routes: list[Address] = []
    rejected: list[Address] = []


# ---- after the handshake


class In(Strict):
    """A message from a person to their computer. Only ever treated as user input."""

    t: Literal["in"] = "in"
    id: FrameId
    sender: Address
    text: Annotated[str, Field(max_length=MAX_TEXT_CHARS)] = ""
    button: Annotated[str, Field(pattern=ID_PATTERN)] | None = None
    name: Annotated[str, Field(max_length=80)] = ""


class Out(Strict):
    """A message from the computer to a person."""

    t: Literal["out"] = "out"
    id: FrameId
    to: Address
    text: Annotated[str, Field(max_length=MAX_TEXT_CHARS)] = ""
    buttons: Annotated[list[Button], Field(max_length=MAX_BUTTONS)] = []
    image: Image | None = None


class Typing(Strict):
    """A reply to this person is coming (SPEC §3.3): sent every few seconds while it lasts; on=False ends it."""

    t: Literal["typing"] = "typing"
    to: Address
    on: bool = True


class PairStart(Strict):
    t: Literal["pair_start"] = "pair_start"


class PairCode(Strict):
    t: Literal["pair_code"] = "pair_code"
    code: Annotated[str, Field(pattern=r"^[A-Z2-9]{3}-[A-Z2-9]{3}$")]
    expires_in: int


class Paired(Strict):
    t: Literal["paired"] = "paired"
    cert: Cert
    name: Annotated[str, Field(max_length=80)] = ""


class Unpair(Strict):
    t: Literal["unpair"] = "unpair"
    address: Address


class Renew(Strict):
    t: Literal["renew"] = "renew"
    cert: Cert


class Renewed(Strict):
    t: Literal["renewed"] = "renewed"
    cert: Cert


class Notice(Strict):
    """Gateway → computer information, e.g. a rejected frame. Never user content."""

    t: Literal["notice"] = "notice"
    code: Annotated[str, Field(pattern=r"^[a-z_]{1,40}$")]
    detail: Annotated[str, Field(max_length=200)] = ""


class Ping(Strict):
    t: Literal["ping"] = "ping"


class Pong(Strict):
    t: Literal["pong"] = "pong"


ToComputer = Annotated[In | PairCode | Paired | Renewed | Notice | Ping | Pong, Field(discriminator="t")]
ToGateway = Annotated[Out | Typing | PairStart | Unpair | Renew | Ping | Pong, Field(discriminator="t")]
TO_COMPUTER = TypeAdapter(ToComputer)
TO_GATEWAY = TypeAdapter(ToGateway)
HELLO = TypeAdapter(Hello)
AUTH = TypeAdapter(Auth)
READY = TypeAdapter(Ready)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def frame_too_big(raw: str) -> bool:
    """Frames carrying an image may be up to 2 MB; every other frame up to 1 MB (SPEC §6.2)."""
    size = len(raw.encode())
    if size > MAX_IMAGE_FRAME_BYTES:
        return True
    return size > MAX_FRAME_BYTES and '"image":{' not in raw


def normalise_phone(text: str) -> str | None:
    """'+44 7900 000001' or '447900000001' → '+447900000001'; None if it isn't an international number."""
    digits = re.sub(r"[\s().-]", "", text.strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = "+" + digits
    return digits if re.fullmatch(r"\+[1-9]\d{6,14}", digits) else None
