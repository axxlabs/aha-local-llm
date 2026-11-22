# aha-local-llm

**aha! A Truly Private Personal Assistant.** It runs on your own computer, and you talk to it from the
[Aha! app](https://github.com/axxlabs/aha-app) on your phone or laptop. Every message is **end-to-end
encrypted** between your device and your computer (hybrid post-quantum pairing, then a Double Ratchet). The
model, your memories, your mail tokens and your conversation history stay on your computer. Before anything
leaves with your words or data in it (typing into a website, an email) and before Aha! saves something about
you, it shows you a card with the exact content: Approve, Edit, Decline.

What it does: searches and reads the web (citing pages), uses websites in its own browser (bookings, forms,
screenshots sent to your chat), signs in to sites when you want (you type your password on a card; the model
never sees it) and stays signed in: each person's browser remembers cookies and site data like their own does,
encrypted on this computer, until they forget a site or clear it in Settings. When a site still asks "are you
human?", you tap the check on a card. It reads and writes your Gmail, reads photos you send
(receipts, tickets), reminds you, runs tasks on a schedule and watches for changes (quiet until there's news),
and remembers what you approve. Each person in the household has their own account, memories, browser and mail.

This repo is the part that runs on your computer. Messages reach it through an
[Aha! gateway](https://github.com/axxlabs/aha-gateway) (self-hosted, or the hosted one for $5/month per user),
which carries encrypted messages it can't read and keeps nothing. Website: https://axxlabs.ai/aha/

## Install with a coding agent (easiest)

Open Claude Code (or another coding agent) on the computer that will run aha and say:

> Install Aha! for me from https://github.com/axxlabs/aha-local-llm. Follow its AGENTS.md, section "Setting up aha for a user".

It checks your machine, asks you a few questions (which gateway, your household's name and timezone), downloads the
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
| `aha-browser` | Playwright MCP, Chromium in a virtual display (Xvfb) with your household's language and timezone, so sites see an ordinary browser; a separate browser per person, its tabs closed after 30 minutes unused; what it remembers (cookies, site data) is kept by the agent, encrypted | internet |
| `aha-agent` | this repo: agent loop, tools, policy, approvals, memories, end-to-end encryption, tunnel | both |

All are rootless Podman containers managed by systemd Quadlet units (`deploy/quadlet/`), with capabilities
dropped and read-only file systems where the image allows. Your data lives in `~/.local/share/aha/`;
the configuration in `~/.config/aha/config.toml`.

## Email (Gmail)

Each person can connect their own Gmail. Aha! then searches and reads it, and writes, replies and saves drafts;
every email is shown to them in full, editable, and goes only when they approve. After a task has read mail,
every web search and page address in that task is shown first too, so a message can't steer Aha! into sending
their data somewhere. The connection uses Google's OAuth with read and compose permissions only (nothing can
delete or change mail), and the token is kept encrypted in that person's folder.

Setting it up takes about 15 minutes, once per household, on the computer:

1. **Make the household's Google app** at https://console.cloud.google.com (free, any Google account):
   - create a project and enable the **Gmail API** (APIs & Services → Library);
   - under Google Auth Platform: **Branding** (app name "Aha!", your email), **Audience** "External", then
     **Publish app** so it reads "In production" (in "Testing", Google ends every sign-in after 7 days; it stays
     "unverified", which is fine for a household of up to 100 people);
   - **Data access**: add the scopes `https://www.googleapis.com/auth/gmail.readonly` and
     `https://www.googleapis.com/auth/gmail.compose`;
   - **Clients**: create a client of type **Desktop app** and download its JSON file.
2. `aha mail setup --client ~/Downloads/client_secret_….json` (it's stored encrypted; delete the download).
3. For each person: `aha mail connect <person>`. They open the link it prints and sign in with Google; past
   "Google hasn't verified this app" (Advanced → Go to Aha!), they tick both permissions. On a phone, the last
   page won't load: copy its address into the terminal. `aha mail status` shows who's connected;
   `aha mail disconnect <person>` revokes it.

If Aha! later says email isn't connected for someone who had connected (after a password change, removing
the app in their Google account, or six months unused), run `aha mail connect <person>` again.

## Encryption at rest

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
