# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The agent process (runs in the aha-agent container): tools, agent, tunnel and the admin socket (SPEC §3.9)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from . import __version__
from .accounts import Accounts
from .agent import Agent
from .certs import load_or_create_key, public_b64
from .config import Config
from .e2e import crypto
from .e2e.channel import Channel
from .llm import LLM
from .tools import browser, conversation, documents, memory, web
from .tools.registry import Registry
from .tunnel import Tunnel

log = logging.getLogger("aha")


class Service:
    def __init__(self, config: Config) -> None:
        self.config = config
        data = config.data_dir
        self.key = load_or_create_key(data / "keys" / "instance.key")
        self.accounts = Accounts(data)
        self.registry = Registry()
        for module in (web, memory, documents, conversation):
            module.register(self.registry)
        self.browser = browser.BrowserSession(config.browser_url)
        self.llm = LLM(config.llm_url, config.llm_model)
        self.channel = Channel(
            self.accounts,
            data / "keys",
            self._send_frame,
            config.app_url,
            config.gateway_device_url,
            config.household_name,
            config.history_sync_days,
            config.push_preview,
            config.llm_model,
        )
        self.tunnel = Tunnel(
            config.gateway_url, config.gateway_pin, self.key, self.accounts, config.household_name, self.channel
        )
        self.agent = Agent(config, self.accounts, self.llm, self.registry, self.channel, self.browser)
        self.channel.agent = self.agent
        self.started = time.time()

    async def _send_frame(self, frame) -> None:
        await self.tunnel.send(frame)

    async def run(self) -> None:
        self.browser.start()
        count = await browser.register(
            self.registry, self.browser, self.config.data_dir / "browser" / "pinned-tools.json"
        )
        log.info("aha %s: %d tools (%d browser)", __version__, len(self.registry.names()), count)
        sock = self.config.data_dir / "agent.sock"
        sock.unlink(missing_ok=True)
        admin = await asyncio.start_unix_server(self._admin, path=str(sock))
        os.chmod(sock, 0o600)
        async with admin:
            await self.tunnel.run()

    # ---- admin socket: one JSON request per connection, one JSON reply

    async def _admin(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await asyncio.wait_for(reader.readline(), 10))
            reply = await self._handle(request)
        except Exception as e:
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()

    async def _handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            return {
                "ok": True,
                "version": __version__,
                "instance": public_b64(self.key),
                "fingerprint": crypto.short_fingerprint(self.channel.identity.fingerprint()),
                "gateway": self.config.gateway_url,
                "tunnel": self.tunnel.status,
                "accounts": len(self.accounts.all()),
                "devices": sum(len(a.devices) for a in self.accounts.all()),
                "online": len(self.channel.online),
                "tools": len(self.registry.names()),
                "running": len(self.agent.running),
                "uptime": int(time.time() - self.started),
            }
        if cmd == "pair":
            name = str(req.get("name", "")).strip()
            if not name:
                return {"ok": False, "error": "give the person's name: aha pair <name>"}
            acct = self.accounts.find(name) or self.accounts.create(name)
            code = await self.tunnel.pair_code()
            state = self.channel.new_pairing(code, acct)
            return {
                "ok": True,
                "code": code,
                "account": acct.id,
                "link": self.channel.pairing_link(code, state, acct, self.config.gateway_pin),
            }
        if cmd == "wait_paired":
            code = str(req.get("code", ""))
            state = self.channel.pairings.get(code)
            if state is None:
                return {"ok": False, "error": "no such pairing (it may have finished or expired)"}
            name = await asyncio.wait_for(asyncio.shield(state.done), float(req.get("timeout", 600)))
            return {"ok": True, "name": name}
        if cmd == "accounts":
            return {
                "ok": True,
                "accounts": [
                    {
                        "id": a.id,
                        "name": a.name,
                        "devices": len(a.devices),
                        "memories": len(a.load_memory()),
                    }
                    for a in self.accounts.all()
                ],
            }
        acct = self.accounts.find(str(req.get("account", "")))
        if cmd in ("devices", "revoke", "memory_list", "memory_import", "export", "rename", "delete") and acct is None:
            return {"ok": False, "error": "no such account (see `aha accounts`)"}
        if cmd == "devices":
            return {
                "ok": True,
                "devices": [
                    {
                        "id": d.dev,
                        "name": d.name,
                        "created": d.created,
                        "last_seen": d.last_seen,
                        "online": d.dev in self.channel.online,
                        "push": bool(d.push),
                    }
                    for d in acct.devices.values()
                ],
            }
        if cmd == "revoke":
            dev = str(req.get("device", ""))
            matches = [d for d in acct.devices if d == dev or d.startswith(dev) or acct.devices[d].name == dev]
            if len(matches) != 1:
                return {"ok": False, "error": "no single device matches (see `aha devices <account>`)"}
            await self.channel.revoke(acct, matches[0])
            return {"ok": True}
        if cmd == "rename":
            self.accounts.rename(acct, str(req.get("name", "")))
            return {"ok": True}
        if cmd == "memory_list":
            return {"ok": True, "memories": acct.load_memory()}
        if cmd == "memory_import":
            added = [memory.add_fact(acct, str(m.get("text", ""))) for m in req.get("memories", []) if m.get("text")]
            return {"ok": True, "added": len(added)}
        if cmd == "export":
            target = self.config.data_dir / "exports" / f"{acct.id}.zip"
            target.parent.mkdir(exist_ok=True)
            self.accounts.export(acct.id, target)
            return {"ok": True, "file": target.name}
        if cmd == "delete":
            for dev in list(acct.devices):
                await self.channel.revoke(acct, dev)
            return {"ok": self.accounts.delete(acct.id) is not None}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("AHA_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    for noisy in ("httpx", "mcp", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(Service(Config.load()).run())
