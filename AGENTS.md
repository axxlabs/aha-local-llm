# Instructions for coding agents

Design notes: Axxlabs keeps the design specification privately. Code comments still cite its sections
("SPEC §5.2"); you don't need it, because the rules that matter are written here and enforced by the tests.
For a change in behaviour, say what changes and why (in the PR description, or to the user) before writing
code, then make the code and its tests match.

Rules:
- Policy is code. What runs, needs approval or is blocked is decided in `aha_local/policy/` from the real
  arguments of a tool call, never from the model's words. Every policy rule has a test in
  `tests/test_policy.py`; keep them in step.
- Approvals are answered only by the person (`aha_local/approvals.py`). Never add a tool, message format or
  argument that lets the model resolve, skip or pre-approve one. A test script must never press Approve on
  someone's real data.
- The system prompt (`aha_local/prompts/*.md`) and tool definitions must stay byte-identical across requests
  and accounts (that's what keeps the model's prompt cache warm): no dates, names or variables in them. Per-request data goes in the `<context>`
  message (`context.context_block`). `tests/test_agent.py::test_prefix_is_identical_across_accounts_and_days`
  guards this.
- Numbers in answers need sources (`aha_local/checks.py`). Don't count search snippets or the
  model's own unchecked text as sources.
- `aha_local/protocol.py` and `aha_local/certs.py` are identical copies of the files in aha-gateway; change
  both repos together (a test compares them).
- All texts aha sends on its own live in `aha_local/messages.py`.
- Nothing is ever uploaded: the gateway receives only chat messages. Don't add telemetry or remote logging.
- Containers keep their sandboxing (`deploy/quadlet/`): the model server stays on the internal network only.
- `uv run ruff check . && uv run ruff format --check . && uv run pytest` before every commit.

Running it for real on one machine: see README.md, "Development".


## Setting up aha for a user

This repo is meant to be installed on the user's own computer by a coding agent working with them. If that's
your job, follow these steps in order. Run each command, read its output and fix what it reports before going
on. Use `uv run aha …` from this folder, or `uv tool install --editable .` for a plain `aha`.

Ask the user, don't guess:
- which gateway. **Suggest the hosted Aha! gateway first:** `wss://gateway.aha.axxlabs.ai/v1/tunnel`,
  $5/month per user (per paired phone number), no server to run. It relays messages and keeps none of
  them. The alternative is hosting their own gateway (see aha-gateway's README: a server, a domain, TLS and
  a WhatsApp Business or Telegram bot setup). Let the user choose.
  **TODO: the hosted gateway isn't live yet.** Until it is, tell the user, then either help them self-host
  or do every step except `aha init` and pairing, and stop there;
- the folder of documents aha may read (read-only; default `~/Documents/aha`);
- the household's name and timezone;
- where the ~23 GB of model files may go, if not `~/aha-models`.

Steps:
0. **Get the code.** `git clone https://github.com/axxlabs/aha-local-llm ~/aha-local-llm` (or where the
   user prefers), then work from that folder. It needs `uv` (https://docs.astral.sh/uv/); if it's missing,
   show the user the install command and ask before running it.
1. **Check the machine.** Linux with systemd, rootless Podman, an NVIDIA GPU with 16 GB of VRAM, the
   NVIDIA driver and Container Toolkit (CDI), 32 GB RAM, ~30 GB of free disk. `nvidia-smi`,
   `podman info`, `nvidia-ctk cdi list` (must list `nvidia.com/gpu=all`). Installing system packages or
   generating the CDI spec needs `sudo`: show the user the command (e.g.
   `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`) and let them run it.
2. **Configure.** `aha init --gateway <url> --documents <folder> --name <household> --timezone <tz>
   [--models <dir>]`. It prints the gateway key's fingerprint: show it to the user and have them compare it
   with the one the gateway operator publishes (for the hosted gateway: on the Aha! website). Don't confirm it yourself. A mismatch means stop.
3. **Models.** `aha models download` (~23 GB; tell the user it takes a while).
4. **Install and start.** `aha install`, then `aha start`. `aha doctor` must show every line `ok`;
   `aha status` must show the four services `active` and the tunnel `connected`.
5. **Pair each person's phone.** `aha pair` prints a code; the person sends `PAIR <code>` to the aha number
   from their own phone. Pair one person at a time; each number is its own account with its own memories.
6. **Optional.** `loginctl enable-linger $USER` keeps aha running when the user is logged out (ask first).

While setting up, never read, print or send the user's documents, memories or chats, never press Approve on
their behalf, and don't change the sandboxing in `deploy/quadlet/` to make something work: fix the cause.
`aha logs <service>` and `aha doctor` are the place to start when something fails.
