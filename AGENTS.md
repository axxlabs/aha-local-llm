# Instructions for coding agents

`SPEC.md` in the parent folder (aha-root) is the single source of truth. Read the sections a change touches
before writing code: §3 (the computer side), §4 (context), §5 (policy and approvals), §6 (protocol), §9
(security).

Rules:
- Policy is code. What runs, needs approval or is blocked is decided in `aha_local/policy/` from the real
  arguments of a tool call, never from the model's words. Every policy row in SPEC §5.2 has a test in
  `tests/test_policy.py`; keep them in step.
- Approvals are answered only by the person (`aha_local/approvals.py`). Never add a tool, message format or
  argument that lets the model resolve, skip or pre-approve one. A test script must never press Approve on
  someone's real data.
- The system prompt (`aha_local/prompts/*.md`) and tool definitions must stay byte-identical across requests
  and accounts (SPEC §4.2): no dates, names or variables in them. Per-request data goes in the `<context>`
  message (`context.context_block`). `tests/test_agent.py::test_prefix_is_identical_across_accounts_and_days`
  guards this.
- Numbers in answers need sources (SPEC §3.5.1, `aha_local/checks.py`). Don't count search snippets or the
  model's own unchecked text as sources.
- `aha_local/protocol.py` and `aha_local/certs.py` are identical copies of the files in aha-gateway; change
  both repos together (a test compares them).
- All texts aha sends on its own live in `aha_local/messages.py`.
- Nothing is ever uploaded: the gateway receives only chat messages. Don't add telemetry or remote logging.
- Containers keep their sandboxing (SPEC §3.6): the model server stays on the internal network only.
- `uv run ruff check . && uv run ruff format --check . && uv run pytest` before every commit.

Running it for real on one machine: see README.md, "Development".
