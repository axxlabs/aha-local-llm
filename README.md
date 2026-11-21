# aha-local-llm

**Aha! Your private agent.** It runs on your own computer, and you talk to it from the
[Aha! app](https://github.com/axxlabs/aha-app) on your phone or laptop. Every message is **end-to-end
encrypted** between your device and your computer (hybrid post-quantum pairing, then a Double Ratchet). The
model, your memories, your documents and your conversation history never leave your computer. Before Aha!
types anything into a website or saves something about you, it asks you with a card: Approve, Edit, Decline.

This repo is the part that runs on your computer. Messages reach it through an
[Aha! gateway](https://github.com/axxlabs/aha-gateway) (self-hosted, or the hosted one for $5/month per user),
which carries encrypted messages it can't read and keeps nothing. Website: https://axxlabs.ai/aha/

## Install with a coding agent (easiest)

Open Claude Code (or another coding agent) on the computer that will run aha and say:

> Install Aha! for me from https://github.com/axxlabs/aha-local-llm. Follow its AGENTS.md, section "Setting up aha for a user".

It checks your machine, asks you a few questions (which gateway, which documents folder), downloads the
model and sets everything up. It suggests the hosted Aha! gateway ($5/month per user); you can host your own
instead.

## What you need

- Linux with rootless Podman and systemd (tested on Fedora 44)
- An NVIDIA GPU with 16 GB of VRAM, the NVIDIA driver and the Container Toolkit (CDI)
- 32 GB of RAM or more, about 30 GB of disk for the model

## Install

```sh
uv tool install .                     # or: uv run aha …  from this folder
aha init --gateway wss://<gateway>/v1/tunnel   # pins the gateway's key; check the fingerprint
aha models download                   # Qwen3.6-35B-A3B + vision file, ~23 GB
aha install                           # builds the agent image, installs the systemd units
aha start
aha pair Ana                          # shows a QR code: scan it with Ana's phone to pair it
```

`aha status`, `aha logs`, `aha accounts`, `aha devices <name>`, `aha revoke <name> <device>`,
`aha memory list <name>`, `aha doctor` and `aha --help` for the rest.
To keep it running while you're logged out: `loginctl enable-linger $USER`.

## How it runs

| Container | What | Network |
|---|---|---|
| `aha-llm` | llama.cpp with Qwen3.6-35B-A3B (vision) | internal only: no internet, no LAN |
| `aha-search` | SearXNG | internet |
| `aha-browser` | Playwright MCP, headless Chromium, fresh profile each session | internet |
| `aha-agent` | this repo: agent loop, tools, policy, approvals, memories, end-to-end encryption, tunnel | both |

All are rootless Podman containers managed by systemd Quadlet units (`deploy/quadlet/`), with capabilities
dropped and read-only file systems where the image allows. Your data lives in `~/.local/share/aha/`;
the configuration in `~/.config/aha/config.toml`.

The data folder is encrypted at rest (ChaCha20-Poly1305, a key per file) under one data key that
`systemd-creds` seals to this computer's TPM and host key (`~/.config/aha/data-key.cred`); systemd unseals it
into the agent's private in-memory credentials folder at start. A copied folder, a backup or a stolen disk
can't be read without this computer, or without the recovery code `aha init` shows once. It doesn't hide data
from whoever controls the running computer: the agent needs plaintext to work. Full-disk encryption (LUKS) is
still recommended; `aha doctor` warns without it. `aha recovery-key --show` shows the code again;
`aha restore` (code on stdin) seals it to a new computer.

## Development

```sh
uv sync
uv run pytest            # no GPU needed: a scripted model stands in for the real one
uv run ruff check . && uv run ruff format --check .
```

For an end-to-end run on one machine: start a development gateway from aha-gateway (`scripts/dev.sh`) and the
app's dev server from aha-app (`npm run dev`), then
`aha init --gateway unix:$XDG_RUNTIME_DIR/aha-gateway/gateway.sock --app http://127.0.0.1:5173`, install, and
`aha pair <name>`: open the printed link in a browser on this machine.

The end-to-end encryption (`aha_local/e2e/`) is checked against aha-app's TypeScript implementation with
shared test vectors (`tests/vectors/`, regenerate with `uv run python scripts/make_vectors.py`).

## Licence

Copyright (C) 2026 Axxlabs Limited. Licensed under the GNU Affero General Public License, version 3 or (at your
option) any later version: see `LICENSE`. If you run a modified version for other people, including over a
network, you must offer them its source (AGPL §13; set `source_url` under `[agent]` in the config).
