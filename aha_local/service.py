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
        self.tunnel = Tunnel(
            config.gateway_url,
            config.gateway_pin,
            self.key,
            self.accounts,
            config.household_name,
            self._inbound,
            self._paired,
        )
        self.agent = Agent(config, self.accounts, self.llm, self.registry, self.tunnel.send_out)
        self.started = time.time()

    async def _inbound(self, frame) -> None:
        await self.agent.inbound(frame)

    async def _paired(self, address: str) -> None:
        acct = self.accounts.get(address)
        if acct:
            await self.agent.send(acct, "Hi! I'm aha, running on your computer. Ask me anything, or say *help*.")

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
                "gateway": self.config.gateway_url,
                "tunnel": self.tunnel.status,
                "accounts": len(self.accounts.paired()),
                "tools": len(self.registry.names()),
                "running": len(self.agent.running),
                "uptime": int(time.time() - self.started),
            }
        if cmd == "pair":
            return {"ok": True, "code": await self.tunnel.pair()}
        if cmd == "wait_paired":
            address = await self.tunnel.wait_paired(float(req.get("timeout", 600)))
            return {"ok": True, "address": address}
        if cmd == "accounts":
            return {
                "ok": True,
                "accounts": [
                    {
                        "address": a.address,
                        "name": a.name,
                        "paired": a.cert is not None,
                        "expires": a.cert.expires if a.cert else None,
                        "memories": len(a.load_memory()),
                    }
                    for a in self.accounts.all()
                ],
            }
        address = req.get("address", "")
        acct = self.accounts.get(address)
        if cmd in ("unpair", "memory_list", "memory_import", "export", "rename") and acct is None:
            return {"ok": False, "error": "no such account (see `aha accounts`)"}
        if cmd == "unpair":
            await self.tunnel.unpair(address)
            return {"ok": True}
        if cmd == "rename":
            acct.name = str(req.get("name", ""))[:60]
            acct.save()
            return {"ok": True}
        if cmd == "memory_list":
            return {"ok": True, "memories": acct.load_memory()}
        if cmd == "memory_import":
            added = [memory.add_fact(acct, str(m.get("text", ""))) for m in req.get("memories", []) if m.get("text")]
            return {"ok": True, "added": len(added)}
        if cmd == "export":
            target = self.config.data_dir / "exports" / f"{acct.id}.zip"
            target.parent.mkdir(exist_ok=True)
            self.accounts.export(address, target)
            return {"ok": True, "file": target.name}
        if cmd == "delete":
            if acct and acct.cert:
                await self.tunnel.unpair(address)
            return {"ok": self.accounts.delete(address)}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("AHA_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    for noisy in ("httpx", "mcp", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(Service(Config.load()).run())
