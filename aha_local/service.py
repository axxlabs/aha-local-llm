# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The agent process (runs in the aha-agent container): tools, agent, tunnel and the admin socket (SPEC §3.3)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

from . import __version__, messages
from .accounts import Accounts
from .agent import Agent
from .certs import public_b64
from .config import Config
from .e2e import crypto
from .e2e.channel import Channel
from .llm import LLM
from .models import ModelChoice
from .rest import RestError, Vault, load_or_create_instance_key
from .tools import browser, conversation, mail, memory, schedule, signins, web
from .tools.registry import Registry
from .tunnel import Tunnel

log = logging.getLogger("aha")
ANNOUNCE_SECONDS = 4  # telling everyone aha is restarting, before it stops
PRUNE_EVERY = 3600


class Service:
    def __init__(self, config: Config) -> None:
        self.config = config
        data = config.data_dir
        self.vault = Vault.open(data, config.data_key_file)
        self.key = load_or_create_instance_key(self.vault, data / "keys" / "instance.key")
        self.accounts = Accounts(self.vault)
        self.registry = Registry()
        for module in (web, memory, mail, schedule, conversation):
            module.register(self.registry)
        self.browser = browser.BrowserPool(config.browser_url, config.browser_idle_seconds)
        self.browser.on_open = self._browser_opened
        self.browser.on_close = self._browser_closing
        self.llm = LLM(config.llm_url, config.llm_model)
        self.models = ModelChoice(self.vault, data, self.llm, config.llm_model)
        self.channel = Channel(
            self.accounts,
            data / "keys",
            self._send_frame,
            config.app_url,
            config.gateway_device_url,
            config.household_name,
            config.history_sync_days,
            config.push_preview,
            self.models,
        )
        self.tunnel = Tunnel(
            config.gateway_url, config.gateway_pin, self.key, self.accounts, config.household_name, self.channel
        )
        self.agent = Agent(config, self.accounts, self.llm, self.registry, self.channel, self.browser)
        self.channel.agent = self.agent
        self.scheduler = schedule.Scheduler(self.accounts, self.agent, config.timezone)
        self.started = time.time()

    async def _browser_opened(self, account_id: str, session) -> None:
        """A person's browser context just opened: put back what their browser remembers (SPEC §3.4)."""
        acct = self.accounts.get(account_id)
        if acct is not None:
            await signins.restore(acct, session)

    async def _browser_closing(self, account_id: str, session) -> None:
        """A person's browser context is about to close (idle, or the agent stopping): save its state first."""
        acct = self.accounts.get(account_id)
        if acct is not None:
            await signins.capture(acct, session)

    async def _prune_history(self) -> None:
        """At start and every hour: each person's history is kept only as long as they chose (SPEC §3.8.1)."""
        while True:
            for account in self.accounts.all():
                try:
                    if account.prune_history():
                        await self.channel.history_changed(account)
                except Exception:
                    log.exception("pruning history failed")
            await asyncio.sleep(PRUNE_EVERY)

    async def _send_frame(self, frame) -> None:
        await self.tunnel.send(frame)

    async def run(self) -> None:
        count = await browser.register(
            self.registry, self.browser, self.config.data_dir / "browser" / "pinned-tools.json"
        )
        self.browser.start()
        self.scheduler.start()
        pruning = asyncio.create_task(self._prune_history())
        warming = asyncio.create_task(self.agent.warm())
        log.info("aha %s: %d tools (%d browser)", __version__, len(self.registry.names()), count)
        sock = self.config.data_dir / "agent.sock"
        sock.unlink(missing_ok=True)
        admin = await asyncio.start_unix_server(self._admin, path=str(sock))
        os.chmod(sock, 0o600)
        try:
            async with admin:
                await self.tunnel.run()
        finally:
            pruning.cancel()
            warming.cancel()
            await self.scheduler.stop()
            await self.browser.stop()

    async def announce_restart(self) -> None:
        """Tell everyone the agent is stopping, so a task cut short isn't a mystery (SPEC §3.3)."""
        for account in self.accounts.all():
            try:
                await self.agent.send(account, messages.RESTARTING)
            except Exception:
                log.warning("couldn't tell an account that aha is restarting")

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
        if cmd == "mail_setup":
            client_id, secret = str(req.get("client_id", "")), str(req.get("client_secret", ""))
            if not client_id.endswith(".apps.googleusercontent.com") or not secret:
                return {"ok": False, "error": "that isn't a Google OAuth client (Desktop app) file"}
            self.vault.write_json(mail.client_file(self.vault), {"client_id": client_id, "client_secret": secret})
            return {"ok": True}
        if cmd == "mail_client":
            client = self.vault.read_json(mail.client_file(self.vault))
            if not client:
                return {"ok": False, "error": "no Google OAuth client yet: run `aha mail setup --client <file>`"}
            return {"ok": True, **client, "scopes": list(mail.SCOPES)}
        if cmd == "mail_status":
            out = []
            for a in self.accounts.all():
                out.append({"name": a.label, "email": mail.address(a)})
            return {"ok": True, "accounts": out}
        acct = self.accounts.find(str(req.get("account", "")))
        needs_account = ("devices", "revoke", "memory_list", "memory_import", "export", "rename", "delete")
        if (cmd in needs_account or cmd in ("mail_connect", "mail_disconnect")) and acct is None:
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
        if cmd == "mail_connect":
            email, token = str(req.get("email", "")), str(req.get("refresh_token", ""))
            if "@" not in email or not token:
                return {"ok": False, "error": "missing the address or the token"}
            self.vault.write_json(acct.dir / "mail.json", {"provider": "gmail", "email": email, "refresh_token": token})
            mail.forget_token(acct.id)
            return {"ok": True}
        if cmd == "mail_disconnect":
            return {"ok": True, "revoked": await mail.disconnect(acct)}
        if cmd == "delete":
            for dev in list(acct.devices):
                await self.channel.revoke(acct, dev)
            await mail.disconnect(acct)
            await self.browser.close(acct.id, save=False)
            return {"ok": self.accounts.delete(acct.id) is not None}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


async def _run_until_stopped(service: Service) -> None:
    """Run until SIGTERM or SIGINT. In the container the agent is PID 1, which ignores SIGTERM unless it
    handles it, so without this `aha stop` waits 10 s and kills it."""
    import signal

    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    stopping: list[asyncio.Task] = []

    async def stop() -> None:
        # While the tunnel is still up: everyone hears why a task stopped. A few seconds at most, well inside
        # the 10 s before podman kills the container.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(service.announce_restart(), ANNOUNCE_SECONDS)
        task.cancel()

    def on_signal() -> None:
        if not stopping:
            stopping.append(asyncio.ensure_future(stop()))

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal)
    try:
        await service.run()
    except asyncio.CancelledError:
        log.info("stopping")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("AHA_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    for noisy in ("httpx", "mcp", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        service = Service(Config.load())
    except RestError as e:
        log.error("can't open the data folder: %s", e)
        raise SystemExit(1) from None
    asyncio.run(_run_until_stopped(service))
