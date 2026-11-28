# Instructions for coding agents

Design notes: Axxlabs keeps the design specification privately. Code comments still cite its sections
("SPEC §5.2"); you don't need it, because the rules that matter are written here and enforced by the tests.
For a change in behaviour, say what changes and why (in the PR description, or to the user) before writing
code, then make the code and its tests match.

Rules:
- Policy is code. What runs, needs approval or is blocked is decided in `aha_local/policy/` from the real
  arguments of a tool call, never from the model's words. Every policy rule has a test in
  `tests/test_policy.py`; keep them in step.
- End-to-end encryption lives in `aha_local/e2e/` (AHA-PQ v1). Never log, print or send keys, ratchet states,
  pairing secrets or decrypted messages; the computer alone decides which devices are trusted (a gateway
  certificate never is). Any change there must keep the shared vectors passing in both repos.
- Approvals are answered only by the person (`aha_local/approvals.py`). Never add a tool, message format or
  argument that lets the model resolve, skip or pre-approve one. A test script must never press Approve on
  someone's real data.
- The system prompt (`aha_local/prompts/core.md` and `chat.md`, the skills' list, and
  `prompts/models/<model id>.md` for the model in use) and tool definitions must stay byte-identical across
  requests and accounts on one model (that's what keeps the model's prompt cache warm): no dates, names or
  variables in them. Per-request data goes in the `<context>` message (`context.context_block`).
  `tests/test_agent.py::test_prefix_is_identical_across_accounts_and_days` and `tests/test_skills.py` guard this.
- Keep the base prompt short. The how-to for a kind of work is a skill (`aha_local/skills/*.md`, a `name`,
  `description`, optional `sites`/`tools` header); what only one model needs goes in that model's file. Skills
  are shipped and reviewed like code: the model never writes one.
- Every tool result passes through `markers.neutralise` (in `Registry.call`): a new harness marker (a bracketed
  line the model is told comes from SHA or the person) gets a pattern there and a test.
- Numbers in answers need sources (`aha_local/checks.py`). Don't count search snippets or the
  model's own unchecked text as sources.
- `aha_local/protocol.py` and `aha_local/certs.py` are identical copies of the files in aha-gateway; change
  both repos together (a test compares them).
- Sign-in secrets (`aha_local/tools/browser_signin.py` `browser_login`, `signins.py`): what the person types on a
  sign-in card goes to the page and nowhere else. Never log, return, store or audit it. It's masked in one place
  each: every tool result in `Registry.call`, every transcript record of the task in the agent's `record`
  (`registry.redact`); don't mask it anywhere else, and don't write the transcript around `record`. Each account's browser state (`signins.py`: cookies, site data, signed-in
  sites) is encrypted in its own folder and only ever restored into that account's browser context; it's
  never saved from a context whose restore failed. `tests/test_signin.py` and `tests/test_browser_state.py`
  guard this.
- Harness browser code (`signins.py`, `human.py`) runs through `browser_run_code_unsafe`, which the model can't
  call. Only validated values go into it: references matching `human.REF`, numbers the harness made, text as
  a JSON string literal. Its results echo the code (typed text included): never pass them on.
- The browser must look like what it is, a person's browser (`deploy/browser/`, the browser unit): headed under
  Xvfb, no User-Agent or fingerprint spoofing, the household's language and timezone. Check changes with
  `scripts/browser_check.py` against a test browser, never one with a person's sign-ins.
- Each account has its own browser context (`BrowserPool`); a tool reaches the browser only through
  `ctx.browser`. Never share a context or a connection between accounts.
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
- which gateway. **Suggest the hosted SHA gateway first:** `wss://gateway.aha.axxlabs.ai/v1/tunnel`,
  $5/month per user (per person paired), no server to run. It carries end-to-end encrypted messages it can't
  read and keeps none of them. The alternative is hosting their own gateway (see aha-gateway's README: a small
  server, a domain and TLS) and serving the app (aha-app). Let the user choose;
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
2. **Configure.** `aha init --gateway <url> --name <household> --timezone <tz> [--language <tag>]
   [--models <dir>]`. `--language` is the browser's language (what sites see); it defaults to this computer's
   own, so ask only if the household uses another. It prints the gateway key's fingerprint: show it to the user and have them compare it
   with the one the gateway operator publishes (for the hosted gateway: on the SHA website). Don't confirm it yourself. A mismatch means stop.
   It also creates the data key and prints a **recovery code** once. Tell the user to write it down or put it
   in their password manager now. Don't copy it anywhere yourself, don't repeat it in chat, and don't run
   `aha recovery-key --show`. An existing install gets its key, and its code, at `aha install`.
3. **Models.** `aha models download` (~23 GB; tell the user it takes a while).
4. **Install and start.** `aha install`, then `aha start`. `aha doctor` must show every line `ok`;
   `aha status` must show the four services `active` and the tunnel `connected`.
5. **Pair each person's devices.** `aha pair <name>` (e.g. `aha pair Ana`) shows a QR code; the person scans
   it with their phone (or opens the link on their laptop) and taps "Pair". Each person is their own account
   with their own memories; run it again with the same name for each extra device. The link holds the pairing
   secret: show it only to that person, never paste it anywhere else.
6. **Optional: email.** Ask whether anyone wants SHA to use their Gmail. If so, follow "Connecting email"
   below; if not, skip it.
7. **Optional.** `loginctl enable-linger $USER` keeps aha running when the user is logged out (ask first).

While setting up, never read, print or send the user's email, memories or chats, never press Approve on
their behalf, and don't change the sandboxing in `deploy/quadlet/` to make something work: fix the cause.
`aha logs <service>` and `aha doctor` are the place to start when something fails.


## Connecting email

SHA reads and writes Gmail through Google's API, per person: each person signs in to their own Gmail, and their
tasks only ever reach their own mailbox. It asks for two permissions, read (`gmail.readonly`) and compose
(`gmail.compose`). It can't delete or change mail or settings. Every email or draft it writes is shown to the
person, editable, and goes only once they approve it. Other providers (Outlook, Fastmail, IMAP) aren't
supported.

Google needs an OAuth client, an "app" registered in Google Cloud. Each household makes its own once. It's
free and takes about 15 minutes. The user does it in their browser; you guide them, one step at a time. The
console's wording changes now and then, so go by meaning, not exact labels.

**A. Make the household's Google app (once).** Any Google account can own it, for example the person
setting up. Everyone in the household can then connect their own Gmail through it.
1. Open https://console.cloud.google.com, accept the terms if asked, and create a project, for example "SHA".
   No billing is needed.
2. Enable the Gmail API: APIs & Services → Library → search "Gmail API" → Enable.
3. Google Auth Platform (or "OAuth consent screen"):
   - **Branding**: app name "SHA", a support email and a developer contact email (theirs).
   - **Audience**: choose "External". Then press **Publish app**, so the status reads "In production". This
     matters: in "Testing", Google ends every sign-in after 7 days. Published, the app stays "unverified".
     That's fine for a household; it allows up to 100 people.
     A Google Workspace account (a company or school domain) can choose "Internal" instead, if everyone
     is in that domain. Its admin may have to allow the app.
   - **Data access**: Add or remove scopes. Find or paste `https://www.googleapis.com/auth/gmail.readonly`
     and `https://www.googleapis.com/auth/gmail.compose`. Add them, then Save. Don't add others.
4. **Clients**: Create client → application type **Desktop app** (not "Web application") → any name →
   Create → **Download JSON**. The file is named like `client_secret_….json`.

**B. Give it to SHA (once).** `aha mail setup --client <path to the downloaded JSON>`. It stores the
client encrypted on this computer. Then have the user delete the downloaded file. Don't open, print or
commit it.

**C. Connect each person.** Each person does this themselves, at this computer if possible:
- Have the user run it in their own terminal: `aha mail connect <person> [--email them@gmail.com]`. In
  Claude Code that's `! uv run aha mail connect <person>`. It prints a Google sign-in link and waits.
- They open the link, pick their Google account and sign in.
- Google warns "Google hasn't verified this app". That's expected: it's the household's own app. Advanced →
  Go to SHA (unsafe).
- They tick **both** permission boxes and press Continue.
- In a browser on this computer, the page then says "Done" and the command finishes.
- Signing in on another device (a phone) is fine too: the last page won't load. They copy that page's
  address (it starts with `http://127.0.0.1`) and paste it into **the terminal** running the command. Never
  into the chat with you.
- `aha mail status` shows each person's connected address.

**D. Check it works.** The person asks SHA in the app, for example "what's my latest email about?".
Don't test it by reading their mail yourself.

**When something goes wrong**
- "Access blocked" or `access_denied` at sign-in: the app is still in Testing (publish it), or a Workspace
  admin blocks it.
- "Some permissions weren't granted": they didn't tick both boxes. Run connect again.
- "didn't return a long-lived token": remove "SHA" at https://myaccount.google.com/permissions and
  connect again.
- `redirect_uri_mismatch`, or setup says the file "isn't a Desktop app client": the client was made as
  another type. Make a Desktop app client (step A4) and run setup again.
- Later, SHA says email isn't connected for someone who had connected: Google ended the sign-in. That
  happens after a password change, when the person removed the app, or after 6 months unused. Run
  `aha mail connect <person>` again.
- To stop: `aha mail disconnect <person>`. It revokes SHA's access at Google and deletes the token.

Never ask for, type or see anyone's Google password or 2-Step code, or the address Google redirects to.
Never read, search or send their email to check the setup.
