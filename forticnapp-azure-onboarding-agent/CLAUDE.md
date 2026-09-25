# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A FastAPI web chat app + two MCP servers that guide a user through onboarding a FortiCNAPP Azure
integration end-to-end: preflight checks, App Registration/SP creation, FortiCNAPP CloudAccount
creation, and verification. The same agent behavior also runs unmodified in Claude Desktop (see
`docs/claude-desktop.md`) — both front ends load `SKILL.md` as the system prompt and talk to the
same two MCP servers.

`SKILL.md` is the actual agent brain: phases, guardrails, decision tables, tool usage. Read it
before touching agent behavior — `app/agent.py` just appends a short runtime addendum to it.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                       # then fill in Bifrost + FortiCNAPP + secret vars
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Local test loop with mocks (no real Azure tenant or FortiCNAPP account needed):

```bash
uvicorn tests.mock_backends:app --port 9000 &
ANTHROPIC_BASE_URL=http://127.0.0.1:9000/anthropic ANTHROPIC_AUTH_TOKEN=sk-bf-test \
LW_ACCOUNT=http://127.0.0.1:9000 LW_API_KEY=lw-key LW_API_SECRET=lw-secret \
FCNAPP_SECRET_AZURE=real-azure-secret uvicorn app.main:app --port 8000

# MCP server alone, driven over stdio like Claude Desktop would (needs the mock on :9000):
python3 tests/test_mcp_stdio.py

# Exercise the Azure tools without a real tenant, using the scripted fake `az`:
PATH=$PWD/tests/fake_az:$PATH ANTHROPIC_BASE_URL=http://127.0.0.1:9000/anthropic ANTHROPIC_AUTH_TOKEN=sk-bf-test \
LW_ACCOUNT=http://127.0.0.1:9000 LW_API_KEY=lw-key LW_API_SECRET=lw-secret \
FCNAPP_SECRET_AZURE=real-azure-secret uvicorn app.main:app --port 8000
```

There is no pytest suite — `tests/test_mcp_stdio.py` is a standalone script (asserts via `print`,
not a test runner) that drives the FortiCNAPP MCP server directly over stdio: dry run, bad
secret_ref, create, duplicate-block. `tests/mock_backends.py` is a scripted fake Bifrost
(`/anthropic/v1/messages`) + fake FortiCNAPP API (`/api/v2/...`) — its `/anthropic/v1/messages`
handler pattern-matches on keywords in the last user/tool message to decide which tool call to
emit next (see the `if "create app" in low: ...` chain), so if the agent's phrasing changes,
update the matching there too. It also exposes `/_calls`, `/_leak` (did any secret ever reach the
LLM?), and `/_trace` for assertions. `tests/fake_az/az` is a case-statement stand-in for the real
Azure CLI, keyed on exact argument strings — extend its `case` when adding new `az` calls, and
prefer piping unmocked shapes through `echo "UNMOCKED: $a" >&2; exit 1` so gaps fail loudly.

Docker: `docker build -t fcnapp-onboarding . && docker run --env-file .env -p 8000:8000 fcnapp-onboarding`

## Architecture

### The two runtimes share everything except the chat surface

- **Web chat app** (`app/main.py` + `static/index.html`): its own MCP client (`app/mcp_hub.py`),
  its own Bifrost-backed LLM loop (`app/llm.py`, `app/agent.py`), and its own write-confirmation UI
  (Confirm/Cancel card).
- **Claude Desktop**: uses Desktop's own MCP client and model picker; write confirmation comes
  from Desktop's native tool-approval prompt plus each write tool's `dry_run` parameter.

Both point at the exact same `mcp_server/forticnapp_mcp.py` and `mcp_server/azure_mcp.py`
processes (spawned over stdio, or via streamable HTTP if `MCP_SERVERS` is set) and the same
`SKILL.md`. Never add app-only behavior to the MCP servers when it belongs in `SKILL.md`, and vice
versa — the MCP servers must stay a valid standalone surface for Claude Desktop.

### Request flow (web app)

```
POST /api/chat -> agent.run_turn()
  loop (max settings.max_agent_steps):
    llm.chat()               # Bifrost call; converts OpenAI-shaped session state <-> Anthropic content blocks
    for each tool_call:
      tools.run_tool()       # local tool, or routed to mcp_hub.hub
    if no tool_calls: return reply
```

`app/agent.py`'s `Session` holds the OpenAI-style message list (`role: system/user/assistant/tool`)
for the lifetime of the process (in-memory `SESSIONS` dict, no persistence). `app/llm.py` converts
that to Anthropic Messages API shape on the way out (`_to_anthropic`) and Anthropic's response back
to OpenAI shape on the way in (`_from_anthropic`), because Bifrost exposes both routes and the rest
of the app is written against the OpenAI shape. If you add a new field to messages, update both
conversion directions or a turn will silently drop it.

### The write gate is the core safety mechanism — it lives in `app/tools.py`, not the MCP servers

Every MCP tool is annotated `readOnlyHint` (see `READ`/`WRITE` `ToolAnnotations` in each MCP server
file). `tools.run_tool()`:
1. Read-only tool → call it, redact, return straight to the model.
2. Write tool → if it has a `dry_run` param, call it with `dry_run=true` first (validation +
   duplicate check + redacted preview from the tool itself); if that isn't `status: dry_run_ok`,
   hand the error straight back to the model with nothing pending. Otherwise stash it in
   `session.pending[action_id]` and return `awaiting_user_confirmation` — the model is told not to
   claim it's done.
3. Only `POST /api/actions/{action_id}` with `decision: confirm` (a real user click) calls
   `execute_action()`, which re-invokes the same tool with `dry_run=false`.

This means: adding a new write tool to either MCP server automatically gets gated, *as long as*
you (a) mark it with the `WRITE` annotation and (b) give it a `dry_run: bool = True` parameter that
does real validation/duplicate-checking and returns `{"status": "dry_run_ok", "preview": ...}`. A
write tool without `dry_run` still gets gated (goes straight to pending with a locally-built
preview) but skips the pre-validation step, so prefer adding `dry_run` support.

### Secrets never reach the LLM or the chat transcript

Two independent redaction layers, both must stay in sync with any new field names:
- `app/ops.py`'s `redact()` / `SECRET_KEY_RX` (matches `secret|password|token|private_?key`, minus
  a `REF_KEY_RX` allowlist for `*_ref`/`*Id`/`*Name`/`*Hint`/etc.) — applied to every FortiCNAPP
  op result and every `az_cli`/`az_cli_write` JSON result.
- `app/tools.py` calls `redact()` again on every MCP tool result and on write-tool previews before
  they're stored as pending actions, so nothing unredacted is ever appended to `session.messages`.

Client secrets are passed as *references* only, resolved server-side inside the FortiCNAPP MCP
process at creation time:
- `secret_ref="env:FCNAPP_SECRET_<NAME>"` — must start with `settings.secret_env_prefix`
  (`SECRET_ENV_PREFIX`, default `FCNAPP_SECRET_`); any other env var name is refused
  (`app/ops.py::resolve_secret`).
- `secret_ref="kv:<vault>/<secret-name>"` — read via `az keyvault secret show` using the host's
  `az login` identity, at the moment of use.

`azure_create_client_secret` (in `mcp_server/azure_mcp.py`) creates the Key Vault-backed version:
it resets the app credential, writes the raw value to a `0600` temp file, pipes that into
`az keyvault secret set --file`, then deletes the temp file and nulls the local variable — the
secret value itself is never returned to the caller, only `secret_ref="kv:<vault>/<name>"`. If you
touch this function, preserve that pattern (no secret in a return value, a log line, or a shell
arg where `ps` could see it).

### Azure CLI is allow-listed by argv prefix, not by a shell

`mcp_server/azure_mcp.py` runs `az` via `subprocess.run` (never a shell) and only accepts argument
lists whose leading words match a tuple in `READ_ALLOW` / `WRITE_ALLOW`. `az rest` is further
restricted to GET against `graph.microsoft.com` / `management.azure.com` only. When adding a new
`az` capability: add the exact leading-word tuple to the right allow-list, and if it's a write,
give the tool a `dry_run` parameter per the write-gate section above. Don't try to allow-list by
regex on the whole command line — the prefix-tuple match is what keeps this auditable.

### Config loading order (`app/config.py`)

`Settings` is a single frozen dataclass built once at import time from, in priority order:
real environment variables → `.env` (custom minimal parser, `setdefault` semantics so real env
wins) → `~/.lacework.toml` (`[default]` or `$LW_PROFILE` section, for `account`/`api_key`/
`api_secret`/`subaccount`, so a machine already set up for the `lacework` CLI needs no `.env` for
FortiCNAPP credentials). `lw_account` accepts a bare account name (expanded to
`<name>.lacework.net`), a full URL, or `http://127.0.0.1:...` for the local mock.

### FortiCNAPP CloudAccounts operations layer (`app/ops.py`)

Shared by the FortiCNAPP MCP server and reused directly by `mcp_server/azure_mcp.py`'s `redact`
import. All validation for `forticnapp_create_azure_cloud_account` happens here before any network
call: GUID-shape checks on `tenant_id`/`client_id`, queue URL shape for `AzureAlSeq`, secret_ref
resolution, then a live duplicate check via `find_azure_integrations` (blocks creating a second
integration of the same `type` for the same `tenant_id`). Preview payloads always mask the secret
as `"***"` regardless of dry_run.

### `SKILL.md` phases map directly onto `app/agent.py` state, not code structure

The six phases (Scoping → Preflight → Inventory → App Registration → Re-validate → Create → Verify)
and the state fields listed under "Conversation state (track this silently)" in `SKILL.md` are
tracked by the LLM in free text, not in `Session` fields — `Session` only stores the raw message
list, `preflight` (the uploaded JSON report), and `pending` actions. If you need the phase state to
be enforced in code rather than by prompt discipline, that's a real architecture change, not a bug
fix — check with the user before doing it.
