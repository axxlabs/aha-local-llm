"""The `aha` command (SPEC §3.9).

Host side: init, install, start/stop, pairing, accounts, memories, export, doctor. They talk to the running
agent through its Unix socket in the data folder. `aha run` is the agent itself (the container's entrypoint).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .config import DEFAULT_TOML, HOME_CONFIG, HOME_DATA, Config

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
QUADLET_DIR = Path.home() / ".config" / "containers" / "systemd"
UNITS = [
    "aha-internal.network",
    "aha-egress.network",
    "aha-llm.container",
    "aha-search.container",
    "aha-browser.container",
    "aha-agent.container",
]
SERVICES = ["aha-agent", "aha-browser", "aha-search", "aha-llm"]
AGENT_IMAGE = "localhost/aha-agent:latest"


def die(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def admin(request: dict, timeout: float = 30) -> dict:
    path = HOME_DATA / "agent.sock"
    if not path.exists():
        die("aha isn't running (no agent socket). Start it with `aha start`.")
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall((json.dumps(request) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    reply = json.loads(buf or b"{}")
    if not reply.get("ok"):
        die(f"aha: {reply.get('error', 'no reply')}")
    return reply


def fingerprint(key_b64: str) -> str:
    digest = hashlib.sha256(key_b64.encode()).hexdigest()[:20].upper()
    return " ".join(digest[i : i + 4] for i in range(0, 20, 4))


def fetch_pin(url: str) -> str:
    import httpx

    if url.startswith("unix:"):
        transport = httpx.HTTPTransport(uds=url[5:])
        base = "http://localhost"
    else:
        parts = urlsplit(url)
        transport = httpx.HTTPTransport()
        base = f"{'https' if parts.scheme == 'wss' else 'http'}://{parts.netloc}"
    with httpx.Client(transport=transport, timeout=10) as c:
        r = c.get(f"{base}/.well-known/aha-gateway.json")
        r.raise_for_status()
        return r.json()["gateway"]


# ---- commands


def cmd_init(a) -> None:
    if HOME_CONFIG.exists() and not a.force:
        die(f"{HOME_CONFIG} already exists (use --force to replace it).")
    pin = a.pin or fetch_pin(a.gateway)
    print(f"Gateway key fingerprint: {fingerprint(pin)}")
    print("Check it matches the one your gateway operator publishes. It's pinned now; aha refuses any other key.")
    HOME_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    HOME_CONFIG.write_text(
        DEFAULT_TOML.format(
            gateway_url=a.gateway,
            pin=pin,
            name=a.name,
            timezone=a.timezone,
            documents=Path(a.documents).expanduser(),
            models_dir=Path(a.models).expanduser(),
        )
    )
    HOME_CONFIG.chmod(0o600)
    HOME_DATA.mkdir(parents=True, exist_ok=True)
    HOME_DATA.chmod(0o700)
    Path(a.documents).expanduser().mkdir(parents=True, exist_ok=True)
    print(f"Wrote {HOME_CONFIG}. Next: `aha install`.")


def render_units(config: Config) -> dict[str, str]:
    gateway_volume = ""
    if config.gateway_url.startswith("unix:"):
        # Mount the socket's folder, not the socket: a restarted gateway makes a new socket file.
        folder = str(Path(config.gateway_url[5:]).parent)
        gateway_volume = f"Volume={folder}:{folder}"
    secret_file = HOME_DATA / "keys" / "search-secret"
    if not secret_file.exists():
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(os.urandom(32).hex())
        secret_file.chmod(0o600)
    values = {
        "@SEARCH_SECRET@": secret_file.read_text().strip(),
        "@DATA@": str(HOME_DATA),
        "@CONFIG@": str(HOME_CONFIG),
        "@MODELS@": str(config.models_dir),
        "@DOCUMENTS@": str(config.documents),
        "@DEPLOY@": str(DEPLOY),
        "@GATEWAY_VOLUME@": gateway_volume,
        "@AGENT_IMAGE@": AGENT_IMAGE,
    }
    out = {}
    for unit in UNITS:
        text = (DEPLOY / "quadlet" / unit).read_text()
        for k, v in values.items():
            text = text.replace(k, v)
        out[unit] = text
    return out


def cmd_install(a) -> None:
    config = Config.load(HOME_CONFIG)
    model = config.models_dir / "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
    if not model.exists():
        die(f"Model not found: {model}\nDownload it (22 GB) and its vision file (0.9 GB) with:\n  aha models download")
    if not shutil.which("podman"):
        die("Podman is needed: sudo dnf install podman")
    print("Building the agent image…")
    subprocess.run(
        ["podman", "build", "-q", "-t", AGENT_IMAGE, "-f", str(DEPLOY / "Containerfile"), str(REPO)], check=True
    )
    QUADLET_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in render_units(config).items():
        (QUADLET_DIR / name).write_text(text)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(f"Installed {len(UNITS)} units in {QUADLET_DIR}. Start with `aha start`.")
    print("To keep aha running when you're logged out: loginctl enable-linger $USER")


def cmd_uninstall(a) -> None:
    subprocess.run(["systemctl", "--user", "stop", *[f"{s}.service" for s in SERVICES]], check=False)
    for unit in UNITS:
        (QUADLET_DIR / unit).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"Removed the units. Your data stays in {HOME_DATA}.")


def systemctl(action: str) -> None:
    subprocess.run(["systemctl", "--user", action, *[f"{s}.service" for s in SERVICES]], check=True)


def cmd_status(a) -> None:
    for s in reversed(SERVICES):
        state = subprocess.run(
            ["systemctl", "--user", "is-active", f"{s}.service"], capture_output=True, text=True
        ).stdout.strip()
        print(f"{s:12} {state}")
    if (HOME_DATA / "agent.sock").exists():
        st = admin({"cmd": "status"})
        print(
            f"\ngateway  {st['gateway']}\ntunnel   {st['tunnel']}\naccounts {st['accounts']} paired"
            f"\ntools    {st['tools']}\nrunning  {st['running']} task(s)\nversion  {st['version']}"
        )


def cmd_logs(a) -> None:
    os.execvp("journalctl", ["journalctl", "--user", "-u", f"aha-{a.service}.service", "-n", "100", "-f"])


def cmd_pair(a) -> None:
    code = admin({"cmd": "pair"})["code"]
    print(f"\n  Send this from the phone to pair:\n\n      PAIR {code}\n\n  (valid for 10 minutes)\n")
    try:
        address = admin({"cmd": "wait_paired", "timeout": 600}, timeout=610)["address"]
        print(f"Paired {address}.")
    except (TimeoutError, OSError):
        print("No pairing yet; the code expires 10 minutes after it was made.")


def cmd_accounts(a) -> None:
    accounts = admin({"cmd": "accounts"})["accounts"]
    if not accounts:
        print("No accounts yet. Pair a phone with `aha pair`.")
    for x in accounts:
        status = "paired" if x["paired"] else "not paired"
        print(f"{x['address']:24} {x['name'] or '-':16} {status:11} {x['memories']} memories")


def cmd_unpair(a) -> None:
    admin({"cmd": "unpair", "address": a.address})
    print(f"Unpaired {a.address}. Its memories and history stay (aha delete removes them).")


def cmd_rename(a) -> None:
    admin({"cmd": "rename", "address": a.address, "name": a.name})


def cmd_memory(a) -> None:
    if a.action == "list":
        for m in admin({"cmd": "memory_list", "address": a.address})["memories"]:
            print(f"[{m['id']}] {m['text']}  ({m['updated']})")
    elif a.action == "import":
        items = json.loads(Path(a.file).read_text())
        n = admin({"cmd": "memory_import", "address": a.address, "memories": items})["added"]
        print(f"Imported {n} memories.")
    elif a.action == "export":
        print(json.dumps(admin({"cmd": "memory_list", "address": a.address})["memories"], indent=1, ensure_ascii=False))


def cmd_export(a) -> None:
    name = admin({"cmd": "export", "address": a.address})["file"]
    print(f"Exported to {HOME_DATA / 'exports' / name}")


def cmd_delete(a) -> None:
    if input(f"Delete everything aha keeps for {a.address}? Type 'delete': ") != "delete":
        die("Not deleted.")
    admin({"cmd": "delete", "address": a.address})
    print("Deleted.")


def cmd_doctor(a) -> None:
    checks = []

    def check(label: str, ok: bool, hint: str = "") -> None:
        checks.append(ok)
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"\n     {hint}" if hint and not ok else ""))

    check("config", HOME_CONFIG.exists(), "run `aha init`")
    if not HOME_CONFIG.exists():
        raise SystemExit(1)
    config = Config.load(HOME_CONFIG)
    check("podman", bool(shutil.which("podman")), "sudo dnf install podman")
    cdi = (
        subprocess.run(["nvidia-ctk", "cdi", "list"], capture_output=True, text=True)
        if shutil.which("nvidia-ctk")
        else None
    )
    check(
        "NVIDIA GPU for containers (CDI)",
        bool(cdi and "nvidia.com/gpu=all" in cdi.stdout),
        "sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml",
    )
    model = config.models_dir / "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
    check(f"model {model.name}", model.exists(), "aha models download")
    check("vision file", (config.models_dir / "Qwen3.6-35B-A3B-mmproj-F16.gguf").exists(), "aha models download")
    check("documents folder", config.documents.exists(), f"mkdir -p {config.documents}")
    check("units installed", all((QUADLET_DIR / u).exists() for u in UNITS), "aha install")
    if a.repin:
        (HOME_DATA / "browser" / "pinned-tools.json").unlink(missing_ok=True)
        print("     browser tool definitions will be re-pinned at the next start (aha restart)")
    if (HOME_DATA / "agent.sock").exists():
        st = admin({"cmd": "status"})
        check(
            f"agent running, tunnel {st['tunnel']}",
            st["tunnel"].startswith("connected"),
            "check the gateway address in the config and `aha logs agent`",
        )
    raise SystemExit(0 if all(checks) else 1)


MODEL_FILES = {
    "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf": "https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
    "Qwen3.6-35B-A3B-mmproj-F16.gguf": "https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/mmproj-F16.gguf",
}


def cmd_models(a) -> None:
    config = Config.load(HOME_CONFIG)
    config.models_dir.mkdir(parents=True, exist_ok=True)
    for name, url in MODEL_FILES.items():
        target = config.models_dir / name
        if target.exists():
            print(f"have {name}")
            continue
        print(f"downloading {name}…")
        subprocess.run(["curl", "-fL", "-C", "-", "-o", str(target), url], check=True)


def cmd_run(a) -> None:
    from .service import main

    main()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="aha", description="aha: your private assistant, on your own computer.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create the configuration and pin the gateway's key")
    s.add_argument("--gateway", default="wss://gateway.aha.axxlabs.ai/v1/tunnel")
    s.add_argument("--pin", help="the gateway's public key, if you have it")
    s.add_argument("--name", default="Home", help="what your household is called in chats")
    s.add_argument("--timezone", default="Europe/London")
    s.add_argument("--documents", default="~/Documents/aha")
    s.add_argument("--models", default="~/aha-models")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("install", help="build the agent and install the systemd units").set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove the systemd units (keeps your data)").set_defaults(fn=cmd_uninstall)
    for action in ("start", "stop", "restart"):
        sub.add_parser(action, help=f"{action} aha").set_defaults(fn=lambda a, x=action: systemctl(x))
    sub.add_parser("status", help="what's running").set_defaults(fn=cmd_status)
    s = sub.add_parser("logs", help="follow a service's log")
    s.add_argument("service", nargs="?", default="agent", choices=["agent", "llm", "browser", "search"])
    s.set_defaults(fn=cmd_logs)
    sub.add_parser("pair", help="pair a phone: prints a code to send from it").set_defaults(fn=cmd_pair)
    sub.add_parser("accounts", help="list accounts").set_defaults(fn=cmd_accounts)
    for name, fn in (("unpair", cmd_unpair), ("export", cmd_export), ("delete", cmd_delete)):
        s = sub.add_parser(name, help=f"{name} an account")
        s.add_argument("address", help="e.g. wa:+447700900123 (see `aha accounts`)")
        s.set_defaults(fn=fn)
    s = sub.add_parser("rename", help="set an account's display name")
    s.add_argument("address")
    s.add_argument("name")
    s.set_defaults(fn=cmd_rename)
    s = sub.add_parser("memory", help="list, import or export an account's memories")
    s.add_argument("action", choices=["list", "import", "export"])
    s.add_argument("address")
    s.add_argument("file", nargs="?")
    s.set_defaults(fn=cmd_memory)
    s = sub.add_parser("doctor", help="check the setup")
    s.add_argument("--repin", action="store_true", help="accept changed browser tool definitions")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("models", help="download the model files")
    s.add_argument("action", choices=["download"])
    s.set_defaults(fn=cmd_models)
    sub.add_parser("run", help="run the agent (used inside its container)").set_defaults(fn=cmd_run)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
