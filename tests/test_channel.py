# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The end-to-end channel with a device played in Python (SPEC §6.4–§6.6, §11.5, §11.7)."""

import asyncio
import base64
import json
import os

import http_ece
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from aha_local.accounts import Accounts
from aha_local.certs import issue_cert, new_key, public_b64
from aha_local.e2e import crypto as c
from aha_local.e2e.channel import Channel
from aha_local.protocol import E2E, CertIssued, CertRequest, DeviceStatus, Pair, Push
from aha_local.rest import Vault


class FakeAgent:
    def __init__(self) -> None:
        self.remembered: dict[str, set[str]] = {}  # account id → the sites their browser keeps data for
        self.said: list[str] = []
        self.photos: list = []
        self.replies: list[tuple] = []
        self.stopped = 0
        self.working = False

    def owes_reply(self, account):
        return self.working

    async def warm(self):
        self.warmed = getattr(self, "warmed", 0) + 1

    async def inbound(self, account, text, image=None):
        self.said.append(text)
        self.photos.append(image)

    async def approval_reply(self, account, approval_id, decision, values=None, note=""):
        self.replies.append((approval_id, decision, values, note))

    async def stop(self, account, quiet=False):
        self.stopped += 1
        self.quiet = quiet

    async def browser_sites(self, account):
        """What the person's browser keeps data for (the real agent reads it from the browser)."""
        from aha_local.tools import signins

        signed = signins.signed_in_sites(account)
        names = self.remembered.get(account.id, set()) | signed
        return [{"site": s, "signed_in": s in signed} for s in sorted(names)]

    async def forget_site(self, account, site):
        from aha_local.tools import signins

        name = signins.site_name(site)
        known = name in self.remembered.get(account.id, set())
        self.remembered.get(account.id, set()).discard(name)
        return await signins.forget(account, None, site) or known

    async def clear_browser(self, account):
        from aha_local.tools import signins

        self.remembered.pop(account.id, None)
        signins.clear(account)


class Rig:
    """A channel, a fake gateway that signs certificates, and helpers to play a device."""

    def __init__(self, tmp_path) -> None:
        self.gw = new_key()
        self.instance = public_b64(new_key())
        self.frames: list = []
        self.accounts = Accounts(Vault(tmp_path / "data", os.urandom(32)))
        self.channel = Channel(
            self.accounts,
            tmp_path / "data" / "keys",
            self.send,
            "https://app.example",
            "wss://gw.example/v1/device",
            "Home",
        )
        self.agent = FakeAgent()
        self.channel.agent = self.agent

    async def send(self, frame) -> None:
        self.frames.append(frame)
        if isinstance(frame, CertRequest):
            cert = issue_cert(self.gw, frame.device, self.instance, frame.tag)
            asyncio.get_running_loop().call_soon(self.channel.on_cert, CertIssued(id=frame.id, cert=cert))

    def take(self, kind):
        found = [f for f in self.frames if isinstance(f, kind)]
        self.frames = [f for f in self.frames if not isinstance(f, kind)]
        return found

    async def pair(self, name="Ana", psk_override=None):
        acct = self.accounts.find(name) or self.accounts.create(name)
        code = "ABC-DEF"
        state = self.channel.new_pairing(code, acct)
        link = self.channel.pairing_link(code, state, acct, public_b64(self.gw))
        data = json.loads(c.b64url_d(link.split("#pair=")[1]))
        assert data["gw"] == "wss://gw.example/v1/device" and data["name"] == name and data["code"] == code
        await self.channel.on_pair(Pair(code=code, body={"t": "hello"}))
        bundle = self.take(Pair)[0].body
        keys = c.DeviceKeys.generate()
        psk = psk_override or c.b64url_d(data["psk"])
        msg, pk, kept = c.device_finish(bundle, c.b64url_d(data["fp"]), psk, keys)
        await self.channel.on_pair(Pair(code=code, body={"t": "finish", **msg}))
        reply = self.take(Pair)[0].body
        if reply["t"] != "ok":
            return reply, None
        c.device_check_computer(pk, c.b64d(reply["mac"]))
        ratchet = c.Ratchet.init_device(
            pk.sk, c.session_ad(keys.ik_pub, kept["ikc"]), kept["ekc"], keys.kem_seed, kept["kemc"]
        )
        return reply, PyDevice(self, acct, keys, ratchet, pk)


class PyDevice:
    def __init__(self, rig, acct, keys, ratchet, pk) -> None:
        self.rig, self.acct, self.keys, self.ratchet, self.pk = rig, acct, keys, ratchet, pk
        self.dev = c.b64e(keys.sig_pub)

    async def send(self, message: dict) -> None:
        await self.rig.channel.on_e2e(E2E(dev=self.dev, box=c.encrypt_json(self.ratchet, message)))

    def inbox(self) -> list[dict]:
        out = []
        for f in self.rig.take(E2E):
            assert f.dev == self.dev
            out.append(c.decrypt_json(self.ratchet, f.box))
        return out


async def test_pairing_then_messages_both_ways(tmp_path):
    rig = Rig(tmp_path)
    reply, phone = await rig.pair()
    cert = json.loads(reply["cert"])
    assert cert["device"] == phone.dev and cert["instance"] == rig.instance
    acct = rig.accounts.find("Ana")
    assert list(acct.devices) == [phone.dev] and acct.devices[phone.dev].name == "New device"
    await phone.send({"t": "hello", "name": "Ana's phone"})
    assert acct.devices[phone.dev].name == "Ana's phone"
    welcome = phone.inbox()
    assert welcome[0]["t"] == "msg" and "Aha!" in welcome[0]["text"] and welcome[0]["seq"] == 1
    await phone.send({"t": "say", "text": "book a table", "cid": "c1"})
    assert rig.agent.said == ["book a table"]
    echo = phone.inbox()
    assert echo[0]["role"] == "me" and echo[0]["cid"] == "c1" and echo[0]["seq"] == 2
    await rig.channel.message(acct, "Which day?")
    assert phone.inbox()[0]["text"] == "Which day?"
    await rig.channel.typing(acct, True)
    assert phone.inbox() == [{"t": "typing", "on": True}]


JPEG = base64.b64encode(b"\xff\xd8\xff\xe0 a fictional receipt").decode()


async def test_a_photo_reaches_the_agent_and_every_device_and_a_fake_one_doesnt(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    await phone.send({"t": "say", "text": "", "cid": "c9", "image": {"mime": "image/jpeg", "b64": JPEG}})
    assert rig.agent.said == [""] and rig.agent.photos[0].b64 == JPEG
    (echo,) = phone.inbox()
    assert echo["role"] == "me" and echo["image"] == {"mime": "image/jpeg", "b64": JPEG}
    for bad in (
        {"mime": "image/jpeg", "b64": base64.b64encode(b"<svg onload=x>").decode()},  # not what it says
        {"mime": "image/svg+xml", "b64": JPEG},
        {"mime": "image/png", "b64": "not base64!"},
        {"mime": "image/jpeg", "b64": base64.b64encode(b"\xff\xd8\xff" + b"0" * 1_100_000).decode()},
    ):
        await phone.send({"t": "say", "text": "look", "image": bad})
        assert phone.inbox() == [{"t": "error", "code": "bad_image"}]
    assert len(rig.agent.said) == 1


async def test_a_wrong_pairing_secret_fails_and_closes_the_code(tmp_path):
    rig = Rig(tmp_path)
    reply, phone = await rig.pair(psk_override=b"\x00" * 32)
    assert reply == {"t": "error", "code": "proof"} and phone is None
    assert rig.accounts.find("Ana").devices == {}
    assert "ABC-DEF" not in rig.channel.pairings
    assert rig.take(CertRequest) == []  # no certificate was ever asked for


async def test_unknown_devices_and_bad_boxes_are_dropped_without_changing_state(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    stranger = public_b64(new_key())
    await rig.channel.on_e2e(E2E(dev=stranger, box=c.b64e(b"x" * 2000)))
    acct = rig.accounts.find("Ana")
    before = json.dumps(acct.devices[phone.dev].ratchet, sort_keys=True)
    box = bytearray(phone.ratchet.encrypt(b'{"t":"say","text":"x"}'))
    box[-1] ^= 1
    await rig.channel.on_e2e(E2E(dev=phone.dev, box=c.b64e(bytes(box))))
    assert json.dumps(acct.devices[phone.dev].ratchet, sort_keys=True) == before
    assert rig.agent.said == []


async def test_sync_and_history(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    acct = rig.accounts.find("Ana")
    for i in range(5):
        await rig.channel.message(acct, f"m{i}")
    phone.inbox()
    await phone.send({"t": "sync_request", "after": 3})
    sync = phone.inbox()[0]
    assert sync["t"] == "sync" and [x["seq"] for x in sync["items"]] == [4, 5, 6] and sync["last"] == 6
    assert sync["working"] is False
    # Seen: the phone was offline for the answer's `typing: false` and showed "working" under it for good.
    rig.agent.working = True
    await phone.send({"t": "sync_request", "after": 6})
    assert phone.inbox()[0]["working"] is True
    await phone.send({"t": "history_request", "before": 4, "limit": 2})
    assert [x["seq"] for x in phone.inbox()[0]["items"]] == [2, 3]
    await phone.send({"t": "sync_request", "after": 0, "days": 0})
    assert phone.inbox()[0]["items"] == []  # this device keeps no cache


async def test_approval_cards_and_replies(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    acct = rig.accounts.find("Ana")
    await rig.channel.card(
        acct, {"t": "approval", "id": "abcd1234", "kind": "action", "title": "Fill in?", "fields": [], "buttons": []}
    )
    assert phone.inbox()[0]["t"] == "approval"
    await phone.send({"t": "approval_reply", "id": "abcd1234", "decision": "approve", "values": ["x"]})
    await phone.send({"t": "approval_reply", "id": "abcd1234", "decision": "hack"})  # unknown decisions are ignored
    assert rig.agent.replies == [("abcd1234", "approve", ["x"], "")]
    await phone.send({"t": "stop"})
    assert rig.agent.stopped == 1
    await phone.send({"t": "cut"})  # [collapse]: a cut item, kept and sent to every device
    cut = phone.inbox()[-1]
    assert cut["t"] == "cut" and cut["item"] is True and isinstance(cut["seq"], int)
    await phone.send({"t": "nonsense"})
    assert phone.inbox()[-1] == {"t": "error", "code": "unknown_type"}


async def test_offline_devices_get_a_sealed_push_without_content(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    sub_key = ec.generate_private_key(ec.SECP256R1())
    point = sub_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = b"\x05" * 16
    b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": b64u(point), "auth": b64u(auth)}}
    await phone.send({"t": "hello", "name": "P", "push": sub})
    phone.inbox()
    await rig.channel.on_device_status(DeviceStatus(dev=phone.dev, online=False))
    acct = rig.accounts.find("Ana")
    await rig.channel.message(acct, "Booked: ref FX7Q2")
    assert rig.take(E2E) == []
    (push,) = rig.take(Push)
    assert push.endpoint == sub["endpoint"]
    plain = http_ece.decrypt(base64.b64decode(push.body), private_key=sub_key, auth_secret=auth, version="aes128gcm")
    payload = c.push_open(phone.pk.push_key, json.loads(plain))
    assert payload == {"seq": acct.seq}  # no text unless push_preview is on
    assert b"FX7Q2" not in plain


async def test_a_bad_push_subscription_is_ignored(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P", "push": {"endpoint": "http://169.254.169.254/x", "keys": {}}})
    assert rig.accounts.find("Ana").devices[phone.dev].push is None


async def test_revoking_another_device(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "Phone"})
    phone.inbox()
    _, laptop = await rig.pair()
    await laptop.send({"t": "hello", "name": "Laptop"})
    laptop.inbox()
    rig.take(E2E)
    await phone.send({"t": "revoke_device", "device": laptop.dev})
    acct = rig.accounts.find("Ana")
    assert list(acct.devices) == [phone.dev]
    from aha_local.protocol import Revoke

    assert rig.take(Revoke) == [Revoke(dev=laptop.dev)]
    await laptop.send({"t": "say", "text": "still here?"})
    assert rig.agent.said == []  # its frames are dropped now


async def test_settings_show_and_forget_what_the_browser_remembers(tmp_path):
    """Settings → Your browser (SPEC §7.4): the sites, Forget one, Clear browser data."""
    from aha_local.tools import logins, signins

    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    acct = rig.accounts.find("Ana")
    rig.agent.remembered[acct.id] = {"waitrose.com", "news.example"}
    signins.signed_in(acct, "waitrose.com", "waitrose.com")
    await phone.send({"t": "browser_request"})
    assert phone.inbox() == [
        {
            "t": "browser",
            "sites": [
                {"site": "news.example", "signed_in": False, "trusted": False, "login_saved": False},
                {"site": "waitrose.com", "signed_in": True, "trusted": False, "login_saved": False},
            ],
        }
    ]
    logins.save(acct, "www.waitrose.com", "ana@example.com", "pw-not-shown")
    await phone.send({"t": "browser_request"})
    sent = phone.inbox()[0]["sites"]
    assert sent[1]["login_saved"] and "pw-not-shown" not in json.dumps(sent) and "ana@" not in json.dumps(sent)
    await phone.send({"t": "login_forget", "site": "waitrose.com"})  # Forget login: the sign-in stays
    assert phone.inbox()[0]["sites"][1] == {
        "site": "waitrose.com",
        "signed_in": True,
        "trusted": False,
        "login_saved": False,
    }
    await phone.send({"t": "browser_forget", "site": "waitrose.com"})
    left = [{"site": "news.example", "signed_in": False, "trusted": False, "login_saved": False}]
    assert phone.inbox()[0]["sites"] == left
    await phone.send({"t": "browser_forget", "site": 5})  # malformed: nothing forgotten
    assert phone.inbox()[0]["sites"] == left
    await phone.send({"t": "browser_clear"})
    assert phone.inbox() == [{"t": "browser", "sites": []}]
    assert not (acct.dir / signins.FILE).exists()


class FakeModelServer:
    """The model server's /models listing, which names the presets of models.ini."""

    url = "http://llm/v1"

    def __init__(self, ids) -> None:
        self.ids, self.model, self.client = ids, "qwen3.6-35b", self

    async def get(self, url, timeout=None):
        import httpx

        return httpx.Response(200, json={"data": [{"id": i} for i in self.ids]})


async def test_settings_choose_the_households_model(tmp_path):
    """Settings → Model (SPEC §3.2): offered when the model server lists it, kept, and loaded at once."""
    from aha_local.models import ModelChoice

    rig = Rig(tmp_path)
    llm = FakeModelServer(["qwen3.6-35b", "qwen3.5-9b"])
    rig.channel.models = ModelChoice(rig.accounts.vault, tmp_path / "data", llm, "qwen3.6-35b")
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    await phone.send({"t": "model_request"})
    listing = phone.inbox()[0]
    assert listing["current"] == "qwen3.6-35b"
    ready = {m["id"]: m["ready"] for m in listing["choices"]}
    assert ready["qwen3.6-35b"] and ready["qwen3.5-9b"] and not ready["gemma-4-12b"]  # only what the server lists
    await phone.send({"t": "model_set", "model": "qwen3.5-9b"})
    await asyncio.sleep(0)
    assert phone.inbox()[0]["current"] == "qwen3.5-9b"
    assert llm.model == "qwen3.5-9b" and rig.agent.warmed == 1
    again = ModelChoice(rig.accounts.vault, tmp_path / "data", FakeModelServer([]), "qwen3.6-35b")
    assert again.current == "qwen3.5-9b"  # kept across restarts
    await phone.send({"t": "model_set", "model": "gpt-9"})  # not one of ours
    assert phone.inbox() == [{"t": "error", "code": "bad_model"}]
    llm.ids = ["qwen3.6-35b"]  # its files aren't there: the model server doesn't list it
    await phone.send({"t": "model_set", "model": "qwen3.6-35b"})
    await phone.send({"t": "model_set", "model": "qwen3.5-9b"})
    out = phone.inbox()
    assert out[0]["current"] == "qwen3.6-35b" and out[1] == {"t": "error", "code": "bad_model"}


async def test_settings_trust_sites_and_forget_memories(tmp_path):
    """SPEC §5.7 and §5.4: set only by the person, from Settings."""
    from aha_local.tools import memory, trusted

    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    acct = rig.accounts.find("Ana")
    await phone.send({"t": "browser_trust", "site": "www.waitrose.com", "trusted": True})
    assert phone.inbox() == [
        {"t": "browser", "sites": [{"site": "waitrose.com", "signed_in": False, "trusted": True, "login_saved": False}]}
    ]
    await phone.send({"t": "browser_trust", "site": "192.168.1.1", "trusted": True})
    assert phone.inbox()[0] == {"t": "error", "code": "bad_site"}
    await phone.send({"t": "browser_clear"})
    assert phone.inbox()[0]["sites"][0]["trusted"]  # clearing browser data leaves trust
    await phone.send({"t": "browser_trust", "site": "waitrose.com", "trusted": False, "login_saved": False})
    assert phone.inbox() == [{"t": "browser", "sites": []}] and trusted.sites(acct) == []

    keep, drop = memory.add_fact(acct, "Prefers window seats"), memory.add_fact(acct, "Lives in Leeds")
    await phone.send({"t": "memory_forget", "id": drop["id"]})
    assert phone.inbox() == [{"t": "memories", "items": [{"id": keep["id"], "text": "Prefers window seats"}]}]


@pytest.mark.parametrize(
    "bad", [{"t": "say", "text": 5}, {"t": "say", "text": "  "}, {"t": "sync_request", "after": -1}]
)
async def test_malformed_messages_do_nothing_harmful(tmp_path, bad):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    await phone.send(bad)
    assert rig.agent.said == []


async def test_a_corrupted_ratchet_state_drops_messages_but_keeps_the_channel_alive(tmp_path):
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    acct = rig.accounts.find("Ana")
    acct.devices[phone.dev].ratchet = {"v": 1, "rk": "not base64!"}
    await phone.send({"t": "say", "text": "hello?"})  # must not raise into the tunnel loop
    await rig.channel.message(acct, "still alive")
    assert rig.agent.said == []


async def test_a_new_device_certificate_names_its_pairing_code(tmp_path):
    """The gateway signs only for a device that joined this computer's code (SPEC §6.4)."""
    rig = Rig(tmp_path)
    await rig.pair()
    requests = rig.take(CertRequest)
    assert len(requests) == 1 and requests[0].code == "ABC-DEF"


def test_short_fingerprint_is_what_people_compare():
    fp = bytes(range(32))
    short = c.short_fingerprint(fp)
    assert len(short) == 19 and short.count(" ") == 3 and short.replace(" ", "").isalnum()
    assert short == "AAAQ EAYE AUDA OCAJ"


def test_a_reply_never_outgrows_a_message():
    """Seen 2026-11-22: a sync with many card screenshots was over the 2 MB limit, so a device couldn't sync."""
    from aha_local.e2e.channel import fit

    shot = {"mime": "image/jpeg", "b64": "A" * 300_000}
    items = [{"seq": i, "t": "approval", "title": f"card {i}", "image": shot} for i in range(1, 11)]
    out = fit(items, budget=1_000_000)
    assert [it["seq"] for it in out] == list(range(1, 11))  # every item kept…
    assert [("image" in it) for it in out] == [False] * 7 + [True] * 3  # …the oldest without their pictures
    assert len(json.dumps(out)) <= 1_000_000
    big = [{"seq": i, "t": "msg", "text": "x" * 400_000} for i in range(1, 6)]
    assert [it["seq"] for it in fit(big, budget=1_000_000)] == [4, 5]  # text alone too big: oldest dropped
    assert fit(items[:2]) == items[:2]  # small replies are untouched


async def test_settings_show_and_disconnect_the_persons_email(tmp_path, monkeypatch):
    """Settings → Connections (SPEC §7.4): the addresses only, never a token; Disconnect revokes one."""
    from aha_local.tools import mail

    revoked = []

    async def revoke(account, email=""):
        revoked.append(email)
        mail._save(account, [c for c in mail.connections(account) if c["email"] != email])
        return True

    monkeypatch.setattr(mail, "disconnect", revoke)
    rig = Rig(tmp_path)
    _, phone = await rig.pair()
    await phone.send({"t": "hello", "name": "P"})
    phone.inbox()
    acct = rig.accounts.find("Ana")
    await phone.send({"t": "connections_request"})
    assert phone.inbox() == [{"t": "connections", "email": "", "emails": []}]
    acct.vault.write_json(  # one connection, as stored before there could be several
        acct.dir / "mail.json", {"provider": "gmail", "email": "ana@example.com", "refresh_token": "rt"}
    )
    mail.add_connection(acct, "gmail", "ana.work@example.com", "rt2")
    await phone.send({"t": "connections_request"})
    both = ["ana@example.com", "ana.work@example.com"]
    assert phone.inbox() == [{"t": "connections", "email": "ana@example.com", "emails": both}]
    assert b"rt2" not in (acct.dir / "mail.json").read_bytes()
    await phone.send({"t": "email_disconnect", "email": "ana.work@example.com"})
    assert phone.inbox() == [{"t": "connections", "email": "ana@example.com", "emails": ["ana@example.com"]}]
    assert revoked == ["ana.work@example.com"]


def test_collapse_publishes_a_cut_and_the_next_part_starts_after_it():
    """[collapse] in the app: a cut item for every device; the computer's titles start after it (SPEC §7.4)."""
    from zoneinfo import ZoneInfo

    from aha_local import folding

    items = [
        {"t": "msg", "seq": 1, "ts": 1000},
        {"t": "msg", "seq": 2, "ts": 1060},
        {"t": "cut", "seq": 3, "ts": 1100},
    ]
    assert folding.segment_start(items, ZoneInfo("UTC")) is None  # nothing going on after the cut
    items.append({"t": "msg", "seq": 4, "ts": 1200})
    assert folding.segment_start(items, ZoneInfo("UTC")) == (4, 1200)
