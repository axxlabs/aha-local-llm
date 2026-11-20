# aha-local-llm

**Aha! Your private agent.** It runs on your own computer, and you talk to it from WhatsApp or
Telegram. The model, your memories, your documents and your conversation history never leave your computer.
Before aha types anything into a website or saves something about you, it asks you in the chat, with
buttons: Approve, Edit, Decline.

This repo is the part that runs on your computer. Messages reach it through an
[aha gateway](../aha-gateway) (self-hosted, or the hosted one for $5/month), which relays them and keeps
nothing. The full specification is `SPEC.md` in aha-root.

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
aha pair                              # prints a code; send "PAIR <code>" to aha from your phone
```

`aha status`, `aha logs`, `aha accounts`, `aha memory list <address>`, `aha doctor` and `aha --help` for the rest.
To keep it running while you're logged out: `loginctl enable-linger $USER`.

## How it runs

| Container | What | Network |
|---|---|---|
| `aha-llm` | llama.cpp with Qwen3.6-35B-A3B (vision) | internal only: no internet, no LAN |
| `aha-search` | SearXNG | internet |
| `aha-browser` | Playwright MCP, headless Chromium, fresh profile each session | internet |
| `aha-agent` | this repo: agent loop, tools, policy, approvals, memories, tunnel | both |

All are rootless Podman containers managed by systemd Quadlet units (`deploy/quadlet/`), with capabilities
dropped and read-only file systems where the image allows. Your data lives in `~/.local/share/aha/`;
the configuration in `~/.config/aha/config.toml`.

## Development

```sh
uv sync
uv run pytest            # no GPU needed: a scripted model stands in for the real one
uv run ruff check . && uv run ruff format --check .
```

For an end-to-end run on one machine, start a staging gateway from aha-gateway (`scripts/dev.sh`), then
`aha init --gateway unix:$XDG_RUNTIME_DIR/aha-gateway/gateway.sock` and open http://127.0.0.1:8700/test.

Licence: AGPL-3.0-or-later.
