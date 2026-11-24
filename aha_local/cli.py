# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""The `aha` command (SPEC §3.9).

Host side: init, install, start/stop, pairing, accounts, memories, export, doctor. They talk to the running
agent through its Unix socket in the data folder. `aha run` is the agent itself (the container's entrypoint).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import DEFAULT_TOML, HOME_CONFIG, HOME_DATA, Config, host_language, language_tag, settings_toml

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
DATA_KEY_CRED = HOME_CONFIG.parent / "data-key.cred"
BROWSER_CONFIG = HOME_CONFIG.parent / "browser.json"  # the browser's Playwright MCP configuration (SPEC §3.7)


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


# ---- the data key (SPEC §3.8.1): sealed by systemd-creds, never stored in the clear


def has_tpm() -> bool:
    return subprocess.run(["systemd-analyze", "has-tpm2", "-q"], capture_output=True).returncode == 0


def seal_data_key(key: bytes, target: Path) -> str:
    """Seal the key to this computer (host key + TPM, or host key only without a TPM); returns which."""
    if not shutil.which("systemd-creds"):
        die("systemd-creds is needed to protect the data key (systemd 256 or later).")
    with_key = "host+tpm2" if has_tpm() else "host"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    subprocess.run(
        ["systemd-creds", "--user", "encrypt", f"--with-key={with_key}", "--name=data-key", "-", str(tmp)],
        input=key,
        check=True,
        capture_output=True,
    )
    tmp.chmod(0o600)
    if unseal_data_key(tmp) != key:
        tmp.unlink(missing_ok=True)
        die("Sealing the data key didn't round-trip; nothing was changed.")
    os.replace(tmp, target)
    return with_key


def unseal_data_key(cred: Path) -> bytes | None:
    r = subprocess.run(["systemd-creds", "--user", "decrypt", "--name=data-key", str(cred), "-"], capture_output=True)
    return r.stdout if r.returncode == 0 and len(r.stdout) == 32 else None


def show_recovery_code(key: bytes) -> None:
    from .rest import recovery_code

    print("\nRecovery code for your Aha! data (shown once; `aha recovery-key --show` shows it again):\n")
    print(f"    {recovery_code(key)}\n")
    print("Keep it offline (paper, a password manager). You need it to restore a backup of your data on another")
    print("computer, or after this computer's TPM is cleared. Without it, that data can't be read by anyone.")


def ensure_data_key() -> None:
    if DATA_KEY_CRED.exists():
        return
    key = os.urandom(32)
    with_key = seal_data_key(key, DATA_KEY_CRED)
    print(f"Created the data key, sealed to this computer ({with_key}): {DATA_KEY_CRED}")
    if with_key == "host":
        print("No TPM found: this protects copies and backups of the data folder, but not a stolen disk.")
    show_recovery_code(key)


def cmd_recovery_key(a) -> None:
    if not a.show:
        die("This prints the key that decrypts all your Aha! data. Run `aha recovery-key --show` to see it.")
    key = unseal_data_key(DATA_KEY_CRED) if DATA_KEY_CRED.exists() else None
    if key is None:
        die(f"Can't unseal {DATA_KEY_CRED} on this computer.")
    from .rest import recovery_code

    print(recovery_code(key))


def cmd_restore(a) -> None:
    """Seal a recovery code (read from stdin, never from the command line) to this computer."""
    from .rest import parse_recovery_code

    if DATA_KEY_CRED.exists() and unseal_data_key(DATA_KEY_CRED) is not None and not a.force:
        die(
            f"{DATA_KEY_CRED} already holds a working key; replacing it with --force makes the current data unreadable."
        )
    if sys.stdin.isatty():
        print("Type or paste the recovery code, then Enter:", file=sys.stderr)
    try:
        key = parse_recovery_code(sys.stdin.readline())
    except ValueError as e:
        die(str(e))
    with_key = seal_data_key(key, DATA_KEY_CRED)
    print(f"Sealed the recovery key to this computer ({with_key}). Start aha with `aha restart`.")


def disk_encrypted(path: Path) -> bool:
    src = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "--target", str(path)], capture_output=True, text=True)
    device = src.stdout.strip().split("[", 1)[0]
    if not device:
        return False
    tree = subprocess.run(["lsblk", "-n", "-s", "-o", "TYPE", device], capture_output=True, text=True)
    return "crypt" in tree.stdout.split()


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
            app_url=a.app,
            name=a.name,
            timezone=a.timezone,
            language=language_tag(a.language) if a.language else host_language(),
            models_dir=Path(a.models).expanduser(),
            settings=settings_toml(),
        )
    )
    HOME_CONFIG.chmod(0o600)
    HOME_DATA.mkdir(parents=True, exist_ok=True)
    HOME_DATA.chmod(0o700)
    print(f"Wrote {HOME_CONFIG}.")
    ensure_data_key()
    print("Next: `aha install`.")


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
        "@DEPLOY@": str(DEPLOY),
        "@GATEWAY_VOLUME@": gateway_volume,
        "@AGENT_IMAGE@": AGENT_IMAGE,
        "@DATA_KEY_CRED@": str(DATA_KEY_CRED),
        "@BROWSER_CONFIG@": str(BROWSER_CONFIG),
        "@TIMEZONE@": config.timezone,
        "@LANG@": config.language.replace("-", "_") + ".UTF-8",
    }
    out = {}
    for unit in UNITS:
        text = (DEPLOY / "quadlet" / unit).read_text()
        for k, v in values.items():
            text = text.replace(k, v)
        out[unit] = text
    return out


def render_browser_config(config: Config) -> str:
    """The browser's configuration: launch arguments, the household's language and timezone (SPEC §3.4)."""
    text = (DEPLOY / "browser" / "config.json").read_text()
    text = text.replace("@LANGUAGE@", config.language).replace("@TIMEZONE@", config.timezone)
    json.loads(text)  # a broken template must fail here, not in the browser
    return text


IDLE_POLL = 15  # seconds between looks at the agent while waiting to install


def idle_now(quiet_seconds: int) -> bool:
    """Nobody's task is running, no card waits for anyone, and nothing happened for `quiet_seconds`."""
    st = admin({"cmd": "status"})
    # An agent from before these fields: only whether a task runs is known.
    return not st["running"] and not st.get("cards", 0) and st.get("idle", quiet_seconds) >= quiet_seconds


def cmd_install(a) -> None:
    if not getattr(a, "when_idle", None):
        install()
        return
    # One waiter at a time: it builds when it installs, so it always installs the latest code (seen: two waiters
    # queued minutes apart, the older one restarting the agent again later for nothing).
    lock = open(HOME_DATA / "install.lock", "w")  # noqa: SIM115 (held until the process ends)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another `aha install --when-idle` is already waiting; it will install the latest code.")
        return
    quiet = int(a.when_idle * 60)
    print(f"Waiting until nobody has used Aha! for {a.when_idle:g} minutes and no card waits…", flush=True)
    while True:
        while not idle_now(quiet):
            time.sleep(IDLE_POLL)
        install()
        if idle_now(quiet):  # still nobody: restart now
            break
        print("Someone started using Aha! while it was building; waiting again.", flush=True)
    subprocess.run(["systemctl", "--user", "restart", "aha-agent.service"], check=True)
    print("Restarted the agent with the new build.")


def install() -> None:
    config = Config.load(HOME_CONFIG)
    model = config.models_dir / "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
    if not model.exists():
        die(f"Model not found: {model}\nDownload it (22 GB) and its vision file (0.9 GB) with:\n  aha models download")
    if not shutil.which("podman"):
        die("Podman is needed: sudo dnf install podman")
    print("Building the agent image…")
    build = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, check=False
    ).stdout.strip()
    stamp = ["--build-arg", f"AHA_BUILD={build or 'unknown'}"]
    subprocess.run(
        ["podman", "build", "-q", "-t", AGENT_IMAGE, *stamp, "-f", str(DEPLOY / "Containerfile"), str(REPO)], check=True
    )
    ensure_data_key()  # an install from before encryption at rest gets its key here (SPEC §3.8.1 Upgrade)
    QUADLET_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in render_units(config).items():
        (QUADLET_DIR / name).write_text(text)
    BROWSER_CONFIG.write_text(render_browser_config(config))
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
            f"\ngateway  {st['gateway']}\ntunnel   {st['tunnel']}\naccounts {st['accounts']}"
            f"\ndevices  {st['devices']} ({st['online']} online)"
            f"\ntools    {st['tools']}\nrunning  {st['running']} task(s)\nversion  {st['version']}"
            f"\ncomputer {st.get('fingerprint', '?')}  (fingerprint: compare with the app's Settings)"
        )


def cmd_logs(a) -> None:
    os.execvp("journalctl", ["journalctl", "--user", "-u", f"aha-{a.service}.service", "-n", "100", "-f"])


def print_qr(link: str) -> None:
    import qrcode

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(link)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def cmd_pair(a) -> None:
    reply = admin({"cmd": "pair", "name": a.name})
    print(f"\n  Scan this with {a.name}'s phone (or open the link on their device) to pair it:\n")
    if not a.no_qr:
        print_qr(reply["link"])
    print(f"\n  {reply['link']}\n\n  Valid for 10 minutes, for one device. Keep it private: it is the pairing key.\n")
    try:
        name = admin({"cmd": "wait_paired", "code": reply["code"], "timeout": 600}, timeout=610)["name"]
        print(f"Paired a device for {name}.")
    except (TimeoutError, OSError, SystemExit):
        print("No device paired; the link expires 10 minutes after it was made.")


def cmd_accounts(a) -> None:
    accounts = admin({"cmd": "accounts"})["accounts"]
    if not accounts:
        print("No accounts yet. Pair someone's phone with `aha pair <name>`.")
    for x in accounts:
        print(f"{x['id'][:8]}  {x['name'] or '-':20} {x['devices']} device(s)  {x['memories']} memories")


def cmd_devices(a) -> None:
    import time as _t

    for d in admin({"cmd": "devices", "account": a.account})["devices"]:
        seen = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(d["last_seen"])) if d["last_seen"] else "never"
        state = "online" if d["online"] else "offline"
        print(f"{d['id'][:10]}  {d['name']:24} {state:8} last seen {seen}{'  push' if d['push'] else ''}")


def cmd_revoke(a) -> None:
    admin({"cmd": "revoke", "account": a.account, "device": a.device})
    print("Revoked: that device can no longer talk to aha. Its keys on this computer are deleted.")


def cmd_rename(a) -> None:
    admin({"cmd": "rename", "account": a.account, "name": a.name})


# ---- mail (SPEC §3.4 "Email")


def oauth_code_from(redirect: str, state: str) -> str:
    """The authorization code from the address Google redirected to (pasted, or caught on the loopback)."""
    from urllib.parse import parse_qs

    query = parse_qs(urlsplit(redirect.strip()).query)
    if query.get("error"):
        die(f"Google said: {query['error'][0]}")
    if query.get("state", [""])[0] != state or not query.get("code"):
        die("That isn't the address Google sent you back to (no code, or from another sign-in).")
    return query["code"][0]


def google_sign_in(client: dict, login_hint: str = "") -> dict:
    """Installed-app OAuth with PKCE and a loopback redirect. Returns Google's token reply."""
    import base64
    import secrets
    import select
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlencode

    import httpx

    caught: list[str] = []

    class Catch(BaseHTTPRequestHandler):
        def do_GET(self):
            caught.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Catch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(client["scopes"]),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            **({"login_hint": login_hint} if login_hint else {}),
        }
    )
    print("Open this link and sign in with the Google account to connect:\n")
    print(f"  {url}\n")
    print("On this computer, the page says Done when it's finished. On another device the last page won't load:")
    print("copy its address (it starts with http://127.0.0.1) and paste it here, then Enter.")
    watch = [sys.stdin]
    try:
        while not caught:
            ready, _, _ = select.select(watch, [], [], 0.5)
            if ready:
                line = sys.stdin.readline()
                if not line:
                    watch = []  # no terminal (stdin closed): wait for the browser alone
                elif line.strip():
                    caught.append(line.strip())
    finally:
        server.shutdown()
    code = oauth_code_from(caught[0] if caught[0].startswith("http") else "http://x" + caught[0], state)
    r = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        timeout=20,
    )
    if r.status_code != 200:
        die(f"Google refused the sign-in (HTTP {r.status_code}). Try `aha mail connect` again.")
    return r.json()


def cmd_mail(a) -> None:
    import httpx

    if a.action == "setup":
        if not a.client:
            die("Give the OAuth client file you downloaded from Google Cloud: aha mail setup --client <file>")
        raw = json.loads(Path(a.client).expanduser().read_text())
        info = raw.get("installed") or {}
        if not info:
            die('That file isn\'t a Desktop app client (it has no "installed" section). See `aha mail --help`.')
        admin(
            {
                "cmd": "mail_setup",
                "client_id": info.get("client_id", ""),
                "client_secret": info.get("client_secret", ""),
            }
        )
        print("Saved the Google OAuth client (encrypted on this computer). You can delete the downloaded file.")
        print("Next: aha mail connect <person>")
        return
    if a.action == "status":
        for row in admin({"cmd": "mail_status"})["accounts"]:
            print(f"{row['name']:<20} {', '.join(row.get('emails') or []) or '(not connected)'}")
        return
    if not a.account:
        die(f"Whose mail? aha mail {a.action} <person>")
    if a.action == "disconnect":
        reply = admin({"cmd": "mail_disconnect", "account": a.account, "email": a.email or ""})
        print(
            "Disconnected."
            + (
                ""
                if reply.get("revoked")
                else " (Google didn't confirm the revocation; "
                "remove access at https://myaccount.google.com/permissions to be sure.)"
            )
        )
        return
    client = admin({"cmd": "mail_client"})
    tokens = google_sign_in(client, a.email or "")
    granted = set(tokens.get("scope", "").split())
    if not set(client["scopes"]) <= granted:
        die("Some permissions weren't granted (tick every box on Google's page). Nothing was saved.")
    if not tokens.get("refresh_token"):
        die(
            "Google didn't return a long-lived token. Remove the app at https://myaccount.google.com/permissions "
            "and connect again."
        )
    profile = httpx.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=20,
    )
    profile.raise_for_status()
    email = profile.json()["emailAddress"]
    done = admin(
        {"cmd": "mail_connect", "account": a.account, "email": email, "refresh_token": tokens["refresh_token"]}
    )
    if len(done.get("emails") or []) > 1:
        print(f"{a.account} now has {len(done['emails'])} accounts connected: {', '.join(done['emails'])}.")
    print(
        f"Connected {email} for {a.account}: their mail and calendar. Aha! can now read them, and sends, drafts "
        "or changes events only after they approve each one."
    )


def cmd_memory(a) -> None:
    if a.action == "list":
        for m in admin({"cmd": "memory_list", "account": a.account})["memories"]:
            print(f"[{m['id']}] {m['text']}  ({m['updated']})")
    elif a.action == "import":
        items = json.loads(Path(a.file).read_text())
        n = admin({"cmd": "memory_import", "account": a.account, "memories": items})["added"]
        print(f"Imported {n} memories.")
    elif a.action == "export":
        print(json.dumps(admin({"cmd": "memory_list", "account": a.account})["memories"], indent=1, ensure_ascii=False))


def cmd_export(a) -> None:
    name = admin({"cmd": "export", "account": a.account})["file"]
    print(f"Exported to {HOME_DATA / 'exports' / name}")


def cmd_delete(a) -> None:
    if (
        not a.yes
        and input(f"Delete everything aha keeps for {a.account}, and unpair its devices? Type 'delete': ") != "delete"
    ):
        die("Not deleted.")
    admin({"cmd": "delete", "account": a.account})
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
    check("units installed", all((QUADLET_DIR / u).exists() for u in UNITS), "aha install")
    installed = BROWSER_CONFIG.read_text() if BROWSER_CONFIG.exists() else ""
    check(
        "browser configuration up to date",
        installed == render_browser_config(config),
        "aha install, then aha restart (it has the household's language and timezone, SPEC §3.4)",
    )
    check(
        "data key sealed to this computer",
        DATA_KEY_CRED.exists() and unseal_data_key(DATA_KEY_CRED) is not None,
        "aha install creates it; after a TPM reset or a move, `aha restore` with your recovery code",
    )
    if not disk_encrypted(HOME_DATA):
        print(
            "warn data folder isn't on an encrypted disk\n"
            "     Aha! encrypts its own files, but model files, swap and old plaintext blocks on an SSD aren't;\n"
            "     full-disk encryption (LUKS) covers them."
        )
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
    "Qwen3.5-9B-UD-Q6_K_XL.gguf": "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-UD-Q6_K_XL.gguf",
    "Qwen3.5-9B-mmproj-F16.gguf": "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-F16.gguf",
    "Qwen3.6-35B-A3B-UD-IQ3_XXS.gguf": "https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-IQ3_XXS.gguf",
    "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf": "https://huggingface.co/unsloth/gemma-4-26B-A4B-it-qat-GGUF/resolve/main/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf",
    "gemma-4-26B-A4B-mmproj-F16.gguf": "https://huggingface.co/unsloth/gemma-4-26B-A4B-it-qat-GGUF/resolve/main/mmproj-F16.gguf",
    "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf": "https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF/resolve/main/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",
    "gemma-4-12B-mmproj-F16.gguf": "https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF/resolve/main/mmproj-F16.gguf",
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
    p = argparse.ArgumentParser(
        prog="aha", description="aha! A Truly Private Personal Assistant, on your own computer."
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create the configuration and pin the gateway's key")
    s.add_argument("--gateway", default="wss://gateway.aha.axxlabs.ai/v1/tunnel")
    s.add_argument("--pin", help="the gateway's public key, if you have it")
    s.add_argument("--name", default="Home", help="what your household is called in chats")
    s.add_argument("--timezone", default="Europe/London")
    s.add_argument("--language", help="the browser's language, e.g. en-GB (default: this computer's)")
    s.add_argument("--models", default="~/aha-models")
    s.add_argument("--app", default="https://app.aha.axxlabs.ai", help="where pairing links open the Aha! app")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)
    inst = sub.add_parser("install", help="build the agent and install the systemd units")
    inst.add_argument(
        "--when-idle",
        type=float,
        nargs="?",
        const=3,
        metavar="MINUTES",
        help="wait until nobody has used Aha! for this long (default 3) and no card waits, then build, install "
        "and restart the agent; one waiter at a time",
    )
    inst.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove the systemd units (keeps your data)").set_defaults(fn=cmd_uninstall)
    for action in ("start", "stop", "restart"):
        sub.add_parser(action, help=f"{action} aha").set_defaults(fn=lambda a, x=action: systemctl(x))
    sub.add_parser("status", help="what's running").set_defaults(fn=cmd_status)
    s = sub.add_parser("logs", help="follow a service's log")
    s.add_argument("service", nargs="?", default="agent", choices=["agent", "llm", "browser", "search"])
    s.set_defaults(fn=cmd_logs)
    s = sub.add_parser("pair", help="pair a person's device: shows a QR code to scan")
    s.add_argument("name", help="the person, e.g. Ana (a new name creates their account)")
    s.add_argument("--no-qr", action="store_true", help="print only the link")
    s.set_defaults(fn=cmd_pair)
    sub.add_parser("accounts", help="list accounts").set_defaults(fn=cmd_accounts)
    s = sub.add_parser("devices", help="list a person's devices")
    s.add_argument("account", help="name or id (see `aha accounts`)")
    s.set_defaults(fn=cmd_devices)
    s = sub.add_parser("revoke", help="unpair one device")
    s.add_argument("account")
    s.add_argument("device", help="device id prefix or name (see `aha devices`)")
    s.set_defaults(fn=cmd_revoke)
    for name, fn in (("export", cmd_export), ("delete", cmd_delete)):
        s = sub.add_parser(name, help=f"{name} an account")
        s.add_argument("account", help="name or id (see `aha accounts`)")
        if name == "delete":
            s.add_argument("--yes", action="store_true", help="don't ask for confirmation")
        s.set_defaults(fn=fn)
    s = sub.add_parser("rename", help="set an account's display name")
    s.add_argument("account")
    s.add_argument("name")
    s.set_defaults(fn=cmd_rename)
    s = sub.add_parser("memory", help="list, import or export an account's memories")
    s.add_argument("action", choices=["list", "import", "export"])
    s.add_argument("account")
    s.add_argument("file", nargs="?")
    s.set_defaults(fn=cmd_memory)
    s = sub.add_parser("doctor", help="check the setup")
    s.add_argument("--repin", action="store_true", help="accept changed browser tool definitions")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser(
        "mail",
        help="connect a person's Gmail and Google Calendar",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Gmail, per person. Once per household, make a Google OAuth client:
  1. https://console.cloud.google.com: create a project, enable the Gmail API and the Google Calendar API.
  2. Google Auth Platform: Branding (app name "Aha!"), Audience "External", then Publish app
     ("In production"; it stays unverified, which is fine for your household: Google shows a warning).
  3. Data access: add the scopes gmail.readonly, gmail.compose, calendar.events and
     calendar.calendarlist.readonly.
  4. Clients: create an OAuth client of type "Desktop app" and download its JSON.
  5. aha mail setup --client <that file>
Then for each person: aha mail connect <person>""",
    )
    s.add_argument("action", choices=["setup", "connect", "disconnect", "status"])
    s.add_argument("account", nargs="?", help="the person (for connect and disconnect)")
    s.add_argument("--client", help="for setup: the OAuth client JSON downloaded from Google Cloud")
    s.add_argument(
        "--email",
        help="for connect: suggest this Google account on the sign-in page; for disconnect: only this address "
        "(without it, every connected account of the person)",
    )
    s.set_defaults(fn=cmd_mail)
    s = sub.add_parser("recovery-key", help="show the recovery code for your data key")
    s.add_argument("--show", action="store_true", help="really print it")
    s.set_defaults(fn=cmd_recovery_key)
    s = sub.add_parser("restore", help="seal a recovery code (from stdin) to this computer")
    s.add_argument("--force", action="store_true", help="replace a working key")
    s.set_defaults(fn=cmd_restore)
    s = sub.add_parser("models", help="download the model files")
    s.add_argument("action", choices=["download"])
    s.set_defaults(fn=cmd_models)
    sub.add_parser("run", help="run the agent (used inside its container)").set_defaults(fn=cmd_run)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
