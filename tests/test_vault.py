# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Encryption at rest (SPEC §3.8.1, test 14)."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from aha_local.accounts import Accounts
from aha_local.e2e import crypto
from aha_local.e2e.channel import load_or_create_identity
from aha_local.vault import LOG, RestError, Vault, load_or_create_instance_key, parse_recovery_code, recovery_code

from .conftest import person, say, settle

FIXTURE = Path(__file__).parent / "fixtures" / "state-v1.json"


def all_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


async def test_no_plaintext_in_the_data_folder_after_a_conversation(agent, llm, chat, accounts, config):
    llm.call("memory_add", text="Fe's dentist is Dr Zebedee Quist")
    llm.answer("Booked the table at Quistorante for Zebedee.")
    await say(agent, "remember my dentist, Dr Zebedee Quist")
    card = await chat.next()
    await say(agent, button=card.buttons[0].id)
    await settle(agent)
    load_or_create_identity(accounts.vault, config.data_dir / "keys" / "e2e.json")
    key = load_or_create_instance_key(accounts.vault, config.data_dir / "keys" / "instance.key")
    blob = all_bytes(config.data_dir)
    for secret in (b"Zebedee", b"Quist", b"dentist", b'"summary"', b'"tag"', b'"seq"'):
        assert secret not in blob, secret
    from cryptography.hazmat.primitives import serialization

    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    assert raw not in blob
    assert accounts.vault._key not in blob
    # …and it all reads back.
    assert any("Zebedee" in m["text"] for m in person(accounts).load_memory())
    assert any("Zebedee" in json.dumps(e) for e in person(accounts).read_transcript())
    for p in config.data_dir.rglob("*"):
        if p.is_file() and p.name != "format.json":
            assert p.stat().st_mode & 0o077 == 0, p


def test_files_are_bound_to_their_path(tmp_path):
    v = Vault(tmp_path, os.urandom(32))
    a, b = tmp_path / "accounts" / "a" / "memory.json", tmp_path / "accounts" / "b" / "memory.json"
    v.write_json(a, [{"text": "a's fact"}])
    v.write_json(b, [{"text": "b's fact"}])
    b.write_bytes(a.read_bytes())  # swap a's file into b's place
    with pytest.raises(RestError, match="doesn't decrypt"):
        v.read(b)
    assert v.read_json(a) == [{"text": "a's fact"}]


def test_another_key_and_plaintext_are_refused(tmp_path):
    f = tmp_path / "accounts" / "x" / "context.json"
    Vault(tmp_path, os.urandom(32)).write_json(f, {"summary": "s"})
    with pytest.raises(RestError, match="doesn't decrypt"):
        Vault(tmp_path, os.urandom(32)).read(f)
    f.write_text('{"summary": "planted"}')
    with pytest.raises(RestError, match="isn't encrypted"):
        Vault(tmp_path, os.urandom(32)).read(f)


def _split(raw: bytes) -> list[bytes]:
    out, off = [], len(LOG)
    while off < len(raw):
        (n,) = struct.unpack(">I", raw[off : off + 4])
        out.append(raw[off : off + 4 + n])
        off += 4 + n
    return out


@pytest.mark.parametrize("tamper", ["reorder", "drop_middle", "alter"])
def test_log_records_cant_be_reordered_dropped_or_altered(tmp_path, tamper):
    key = os.urandom(32)
    log = tmp_path / "accounts" / "x" / "transcript.jsonl"
    v = Vault(tmp_path, key)
    for i in range(3):
        v.append(log, f'{{"n": {i}}}'.encode())
    recs = _split(log.read_bytes())
    if tamper == "reorder":
        recs[0], recs[1] = recs[1], recs[0]
    elif tamper == "drop_middle":
        del recs[1]
    else:
        recs[2] = recs[2][:-1] + bytes([recs[2][-1] ^ 1])
    log.write_bytes(LOG + b"".join(recs))
    with pytest.raises(RestError):
        Vault(tmp_path, key).records(log)


def test_a_log_cut_mid_record_still_opens_and_appends(tmp_path):
    key = os.urandom(32)
    log = tmp_path / "accounts" / "x" / "audit.jsonl"
    v = Vault(tmp_path, key)
    v.append(log, b"one")
    v.append(log, b"two")
    log.write_bytes(log.read_bytes()[:-7])  # a crash mid-write
    v2 = Vault(tmp_path, key)
    assert v2.records(log) == [b"one"]
    v2.append(log, b"three")
    assert Vault(tmp_path, key).records(log) == [b"one", b"three"]


def test_a_logs_records_are_bound_to_the_log(tmp_path):
    key = os.urandom(32)
    v = Vault(tmp_path, key)
    a, b = tmp_path / "accounts" / "a" / "transcript.jsonl", tmp_path / "accounts" / "b" / "transcript.jsonl"
    v.append(a, b"a0")
    v.append(b, b"b0")
    b.write_bytes(a.read_bytes())
    with pytest.raises(RestError):
        Vault(tmp_path, key).records(b)


def _old_data_folder(root: Path) -> dict:
    """A data folder as the previous version wrote it: plaintext, from the frozen v1 fixture."""
    fx = json.loads(FIXTURE.read_text())
    acct = root / "accounts" / ("ab" * 16)
    (acct / "devices").mkdir(parents=True)
    (root / "keys").mkdir()
    (root / "browser").mkdir()
    (acct / "account.json").write_text(
        json.dumps({"v": 2, "id": "ab" * 16, "name": "Test Person", "tag": "dGFn", "created": 1, "seq": 7})
    )
    (acct / "memory.json").write_text(json.dumps([{"id": "m1", "text": "likes fictional tea"}]))
    (acct / "transcript.jsonl").write_text(
        "\n".join(json.dumps({"kind": "user", "text": f"hello {i}"}) for i in range(3)) + "\n"
    )
    (acct / "devices" / "d.json").write_text(
        json.dumps({"dev": "DEV", "name": "Phone", "ikd": "", "kemd": "", "push_key": "", "ratchet": {}})
    )
    (root / "keys" / "e2e.json").write_text(json.dumps(fx["identity"]))
    (root / "keys" / "instance.key").write_bytes(b"\x07" * 32)
    (root / "keys" / "search-secret").write_text("ab" * 32)
    (root / "browser" / "pinned-tools.json").write_text("{}")
    return fx


def test_a_plaintext_folder_from_the_previous_version_is_encrypted_and_still_works(tmp_path):
    fx = _old_data_folder(tmp_path)
    key_file = tmp_path.parent / f"{tmp_path.name}-data-key"
    key_file.write_bytes(os.urandom(32))
    v = Vault.open(tmp_path, key_file)
    blob = all_bytes(tmp_path)
    for plain in (b"Test Person", b"fictional tea", b"hello 1", b'"ratchet"', b"\x07" * 32):
        assert plain not in blob, plain
    assert json.loads((tmp_path / "format.json").read_text()) == {"rest": 1}
    assert (tmp_path / "keys" / "search-secret").read_text() == "ab" * 32  # host-side, left plain
    accounts = Accounts(v)
    acct = accounts.all()[0]
    assert (acct.name, acct.seq, list(acct.devices)) == ("Test Person", 7, ["DEV"])
    assert acct.load_memory()[0]["text"] == "likes fictional tea"
    assert [e["text"] for e in acct.read_transcript()] == ["hello 0", "hello 1", "hello 2"]
    acct.transcript({"kind": "user", "text": "after the upgrade"})
    assert acct.read_transcript()[-1]["text"] == "after the upgrade"
    identity = load_or_create_identity(v, tmp_path / "keys" / "e2e.json")
    assert crypto.b64e(identity.fingerprint()) == fx["fingerprint"]
    raw = load_or_create_instance_key(v, tmp_path / "keys" / "instance.key")
    assert raw is not None
    # A second start changes nothing.
    before = all_bytes(tmp_path)
    Vault.open(tmp_path, key_file)
    assert all_bytes(tmp_path) == before


def test_a_backup_opens_elsewhere_only_with_the_recovery_key(tmp_path):
    key = os.urandom(32)
    f = tmp_path / "accounts" / "x" / "memory.json"
    Vault(tmp_path, key).write_json(f, [{"text": "t"}])
    code = recovery_code(key)
    assert len(code.split()) == 13 and all(len(g) == 4 for g in code.split())
    assert Vault(tmp_path, parse_recovery_code(code.lower().replace(" ", "-"))).read_json(f) == [{"text": "t"}]
    with pytest.raises(RestError):
        Vault(tmp_path, os.urandom(32)).read(f)
    with pytest.raises(ValueError):
        parse_recovery_code("not a code")


def test_the_agent_refuses_to_start_without_a_data_key(tmp_path):
    with pytest.raises(RestError, match="no data key"):
        Vault.open(tmp_path, tmp_path / "missing")


def test_export_is_decrypted_for_the_person(accounts, tmp_path):
    import zipfile

    acct = person(accounts)
    acct.transcript({"kind": "user", "text": "export me"})
    acct.save_memory([{"id": "m", "text": "a fact"}])
    out = accounts.export(acct.id, tmp_path / "e.zip")
    with zipfile.ZipFile(out) as z:
        assert b"export me" in z.read("transcript.jsonl")
        assert json.loads(z.read("memory.json")) == [{"id": "m", "text": "a fact"}]
    assert out.stat().st_mode & 0o077 == 0
