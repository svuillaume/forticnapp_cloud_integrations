# FortiCNAPP Azure Onboarding Agent

An interactive assistant that guides a user through a **new FortiCNAPP Azure integration**. It works in five steps:

1. **Preflight:** checks whether this user and tenant can be integrated.
2. **Inventory:** looks for existing FortiCNAPP integrations.
3. **App registration and service principal:** helps create the Azure app and its service principal.
4. **Create:** creates the FortiCNAPP Cloud Account, only after human confirmation.
5. **Verify:** checks the new integration's state.

```
User
  ↓
AI Agent  (app/agent.py, or Claude Desktop)
  ↓
LLM  (Bifrost: qwen3.8-27b-anthropic, …)
  ├── "I need Azure user information"
  ↓
Tool call: az_cli ["ad","signed-in-user","show"]          (MCP server: azure)
  ↓
Agent executes the CLI or script  (allow-listed, no shell; writes need Confirm)
  ↓
Azure  (as the identity signed in with `az login` on the agent host)
  ↓
Tool result  (JSON, secrets redacted)
  ↓
LLM
  ↓
Agent response  →  … later: forticnapp_* tools → FortiCNAPP API v2   (MCP server: forticnapp)
```

There are two ways to run it. Both use the same MCP server and the same `SKILL.md`.

| | Option A: Claude Desktop | Option B: Web chat app |
|---|---|---|
| Chat window | Claude Desktop | Browser (`static/index.html`) |
| Model | Claude (Desktop's picker) | **Bifrost models**; the list is fetched from Bifrost's model endpoint |
| MCP client | Claude Desktop | `app/mcp_hub.py` |
| Write confirmation | Desktop's tool-approval prompt + `dry_run` | Confirm/Cancel card; the model can never write directly |
| Setup | [docs/claude-desktop.md](docs/claude-desktop.md) | below |

## Quickstart (Option B: web chat app)

This walks through running the app on your own machine, step by step. It assumes you don't want
to install the Azure CLI locally and will run Azure-side commands manually in **Azure Cloud
Shell** instead (`ENABLE_AZURE_TOOLS=false`). See "Run Azure tools yourself instead" below if you
do want the agent to run `az` for you.

### 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> `mcp` 2.x renamed an import the code depends on; `requirements.txt` pins `mcp>=1.27,<2`.
> If you ever see `ImportError: cannot import name 'streamablehttp_client'`, re-run
> `.venv/bin/pip install -r requirements.txt` to get back on a compatible 1.x release.

### 2. FortiCNAPP credentials — `~/.lacework.toml`

Skip this if you already use the `lacework` CLI (the app reads the same file).

```bash
cat > ~/.lacework.toml <<'EOF'
[default]
account = "yourtenant"
api_key = "FORTINET_XXXXXXX"
api_secret = "your-secret"
version = 2
EOF
```

If your file already has a **named profile** instead of `[default]` (e.g. `[samv]`), either
rename it to `[default]`, or tell the app which profile to use every time you run it:
`LW_PROFILE=samv` (see step 5).

### 3. LLM backend — Bifrost, or a local model

The app talks to Bifrost by default, but works against any OpenAI-compatible endpoint (local
`llama.cpp`/`llama-server`, vLLM, etc.) — `app/llm.py` just needs `LLM_API=openai` and a base URL.
`scripts/run-backend.sh` switches between them without you having to remember which env vars go
with which backend:

```bash
scripts/run-backend.sh bifrost      # https://bifrost.fabriclab.ca/anthropic (reads ANTHROPIC_* if already exported)
scripts/run-backend.sh llamacpp     # local llama-server on :8080 (see below to set one up)
VLLM_MODEL=<model-id> scripts/run-backend.sh vllm   # local vLLM on :8000
```

It execs `uvicorn` for you, so skip step 5 below when using it — just add your Azure/FortiCNAPP
env vars in front, e.g. `LW_PROFILE=samv ENABLE_AZURE_TOOLS=false scripts/run-backend.sh llamacpp`.
Switching backends means stopping the server (Ctrl+C) and re-running with a different argument —
this is restart-based, not a live in-chat toggle.

To set up a local `llama-server` with tool-calling support (macOS):
```bash
brew install llama.cpp
llama-server -hf bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M --jinja -c 32768 --port 8080
```
`-c 32768` matters: `SKILL.md` + tool schemas + conversation history alone can exceed 14k tokens,
so the default/smaller `-c 8192` fails with `exceed_context_size_error` partway through a session.

Verify tool calls work before relying on it — some model/template combos only answer in prose:
```bash
curl localhost:8080/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
  "messages": [{"role":"user","content":"What is the weather in Paris?"}],
  "tools": [{"type":"function","function":{"name":"get_weather","description":"get weather","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]
}'
```
Look for a populated `tool_calls` array in the response, not just prose text.

Or, to use Bifrost directly without the script — skip this if you already export these for
Claude Code with Bifrost:

```bash
export ANTHROPIC_BASE_URL=https://bifrost.fabriclab.ca/anthropic
export ANTHROPIC_AUTH_TOKEN=sk-bf-...
export ANTHROPIC_DEFAULT_OPUS_MODEL=qwen3.8-27b-anthropic
```

### 4. `.env` — behavior flags only

```bash
cp .env.example .env
```
The defaults (`ALLOW_CREATE=true`, `MAX_AGENT_STEPS=8`, `SECRET_ENV_PREFIX=FCNAPP_SECRET_`) are
usually fine as-is. The one line you do need to set for this walkthrough:
```bash
ENABLE_AZURE_TOOLS=false
```
You don't need `FCNAPP_SECRET_AZURE` yet — only once you're ready to create the FortiCNAPP
Cloud Account (step 7).

### 5. Run it

```bash
LW_PROFILE=samv ENABLE_AZURE_TOOLS=false .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```
(drop `LW_PROFILE=samv` if your `.lacework.toml` uses `[default]`)

Equivalent, and the easier way to remember: `LW_PROFILE=samv ENABLE_AZURE_TOOLS=false scripts/run-backend.sh bifrost`
(see step 3 — same script also switches to `llamacpp`/`vllm`). Only one process can hold port 8000
at a time; stop it (Ctrl+C) before switching backends or re-running.

Left a stray process running from a previous session? `ERROR: [Errno 48] address already in use`
means something is still bound to the port:
```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN     # find the PID (swap 8000 for 8080 for llama-server, etc.)
kill <PID>                           # graceful; add -9 if it won't die
```
Or in one line: `lsof -ti tcp:8000 | xargs kill`.

### 6. Sanity check

```bash
curl localhost:8000/api/config
```
Confirm `"forticnapp_configured": true` and `"mcp_errors": {}`.

### 7. Work through the chat at `http://127.0.0.1:8000`

1. Answer the scoping questions (FortiCNAPP account URL, which subscriptions, which integrations).
2. **Preflight:** download the script from the header link (redirects to the canonical copy at
   [svuillaume/forticnapp_cloud_integrations](https://github.com/svuillaume/forticnapp_cloud_integrations)),
   run it in **Azure Cloud Shell**, then upload the resulting JSON with the "Upload preflight
   report" button.
   - Large tenant? Scope it down first — this is the single biggest time-saver:
     ```bash
     export AZ_SUBSCRIPTIONS="<sub-id-1>,<sub-id-2>"
     export AZ_APP_ID="<existing FortiCNAPP SP appId, if you have one>"
     ./azure_preflight_check.sh
     ```
     (each `export` on its own line — `export VAR=val ./script.sh` is invalid syntax)
   - If the uploaded JSON fails with an "Unexpected non-whitespace character" error, the file
     picked up extra text when it was copied off Cloud Shell (terminal scrollback, not the
     script's fault). Use Cloud Shell's **Manage files → Download**, not copy/paste, to avoid it.
3. Let it check FortiCNAPP inventory for existing integrations before creating anything new.
4. App Registration / service principal / role assignments: since `az` tools are disabled here,
   you run each command yourself in Cloud Shell and report back the results as text (`appId`,
   `tenantId`) — **never paste the client secret into the chat.**
5. Put the client secret in `.env` as `FCNAPP_SECRET_AZURE=<value>` and restart the app; tell the
   chat to use `secret_ref=env:FCNAPP_SECRET_AZURE`.
6. Confirm the Cloud Account creation card when it appears — nothing is created until you click
   Confirm.
7. Ask it to verify the new integration's state.

### Run Azure tools yourself instead

Don't want the manual Cloud Shell round-trip? Two options:

- **Install `az` locally** (`brew install azure-cli`, then `az login`) and set
  `ENABLE_AZURE_TOOLS=true` — the agent then runs `az_cli` / `azure_run_preflight` /
  `az_cli_write` itself, gated by the same Confirm/Cancel card for writes.
- **Or run everything (including `az`/`lacework` CLI) inside a disposable Linux container**,
  keeping your host machine untouched:
  ```bash
  docker build -f Dockerfile.dev -t fcnapp-agent-dev .
  docker run -it --rm -p 8000:8000 -v fcnapp-agent-home:/home/agent --entrypoint bash fcnapp-agent-dev
  # inside the container, once:
  az login                # device-code flow
  lacework configure       # writes ~/.lacework.toml
  # then each time:
  ENABLE_AZURE_TOOLS=true uvicorn app.main:app --host 0.0.0.0 --port 8000
  ```
  The named volume persists `~/.azure` and `~/.lacework.toml` across container restarts.

## Project layout

| Path | Purpose |
|---|---|
| `SKILL.md` | Agent instructions: phases, guardrails, decision table, tools |
| `scripts/forticnapp-azure-preflight.sh` | Bundled copy used by the `azure_run_preflight` MCP tool. Canonical/latest version: [svuillaume/forticnapp_cloud_integrations](https://github.com/svuillaume/forticnapp_cloud_integrations) (`azure_preflight_check.sh`) — zero-touch, read-only Azure preflight (run in Azure Cloud Shell), JSON report |
| `mcp_server/forticnapp_mcp.py` | FortiCNAPP MCP server (stdio or streamable HTTP), 6 CloudAccounts tools |
| `mcp_server/azure_mcp.py` | Azure MCP server: `az_cli` (read), `azure_run_preflight`, `az_cli_write` + `azure_create_client_secret` (gated) |
| `app/` | Web chat backend: FastAPI, Bifrost client, MCP client hub, agent loop, write gate |
| `static/` | Chat window UI (no CDN; the Markdown and sanitizer libraries are bundled in `static/vendor/`) |
| `tests/` | Mock Bifrost + mock FortiCNAPP API, fake `az` CLI (`tests/fake_az`), and an MCP stdio test |
| `examples/sample-preflight-report.json` | Example of the report the agent reads |

## MCP tools (`forticnapp` server)

| Tool | Kind | API |
|---|---|---|
| `forticnapp_list_cloud_accounts` | read | `GET /api/v2/CloudAccounts` |
| `forticnapp_list_cloud_accounts_by_type` | read | `GET /api/v2/CloudAccounts/{type}` |
| `forticnapp_get_cloud_account` | read | `GET /api/v2/CloudAccounts/{intgGuid}` |
| `forticnapp_find_azure_integrations` | read | AzureCfg + AzureAlSeq filtered by `tenantId` |
| `forticnapp_get_cloud_account_schema` | read | `GET /api/v2/schemas/CloudAccounts` |
| `forticnapp_create_azure_cloud_account` | **write** (`dry_run=true` by default) | `POST /api/v2/CloudAccounts` |

## MCP tools (`azure` server)

| Tool | Kind | What it runs |
|---|---|---|
| `az_cli` | read | Allow-listed `az` read commands (account/ad/role/provider/monitor…, `az rest` GET to Graph/ARM only) |
| `azure_run_preflight` | read | `scripts/forticnapp-azure-preflight.sh` → JSON report (issues only, to keep context small) |
| `az_cli_write` | **write** | `ad app create`, `ad sp create`, `ad app permission add/admin-consent`, `role assignment create/delete`, `provider register` |
| `azure_create_client_secret` | **write** | Creates the app secret and stores it straight in Key Vault; returns only `kv:<vault>/<name>` |

**Which Azure identity is used:** whoever ran `az login` on the host that runs the Azure MCP server. For onboarding, that should be the deployer's own delegated login (time-bound Owner / App Admin / PRA through PIM), not a standing high-privilege service principal. Set `ENABLE_AZURE_TOOLS=false` to go back to "user runs the script in Cloud Shell and uploads the JSON".

## Option B: web chat app with Bifrost

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # set ANTHROPIC_* + account/api_key/api_secret + FCNAPP_SECRET_*
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

The app reads the same variables you already use for Claude Code with Bifrost:

```bash
ANTHROPIC_BASE_URL=https://bifrost.fabriclab.ca/anthropic
ANTHROPIC_AUTH_TOKEN=sk-bf-...                          # Bifrost virtual key
ANTHROPIC_DEFAULT_OPUS_MODEL=qwen3.8-27b-anthropic      # default model in the picker
```

The model picker is filled from Bifrost's model list. If the list isn't available, it falls back to the default model.

Docker:

```bash
docker build -t fcnapp-onboarding . && docker run --env-file .env -p 8000:8000 fcnapp-onboarding
```

### Security design
- **Write gate:** every MCP tool not annotated `readOnlyHint=true` is intercepted. The app runs a dry run, shows a Confirm/Cancel card, and executes only after a human clicks Confirm.
- **Secrets stay out of the model:** secrets are passed as references, `kv:<vault>/<name>` (created by `azure_create_client_secret`) or `env:FCNAPP_SECRET_*`, and resolved inside the MCP server. Other environment variables are refused, and every tool result is redacted.
- **Azure CLI allow-list:** `az` runs without a shell, `account get-access-token`, `--debug` and arbitrary `az rest` methods are blocked, and write commands go through the Confirm gate.
- **Idempotent creation:** creating an integration of the same type for the same Azure tenant is blocked.
- **Read-only mode:** `ALLOW_CREATE=false` makes the whole stack read-only.
- **Model thinking hidden:** `<think>` blocks from open models such as Qwen are removed before display.
- **Not for public exposure as-is:** sessions are held in memory and there is no authentication. Put the app behind SSO or a reverse proxy before sharing it.

## Local test with mocks (no real tenant needed)

```bash
uvicorn tests.mock_backends:app --port 9000 &
ANTHROPIC_BASE_URL=http://127.0.0.1:9000/anthropic ANTHROPIC_AUTH_TOKEN=sk-bf-test \
LW_ACCOUNT=http://127.0.0.1:9000 LW_API_KEY=lw-key LW_API_SECRET=lw-secret \
FCNAPP_SECRET_AZURE=real-azure-secret uvicorn app.main:app --port 8000
python3 tests/test_mcp_stdio.py      # MCP server alone (needs the mock on :9000)
# add PATH=$PWD/tests/fake_az:$PATH to the app command to exercise the Azure tools without a tenant
```

## Open items
- Confirm the `AzureCfg` / `AzureAlSeq` payload fields against your tenant's `GET /api/v2/schemas/CloudAccounts`.
- Confirm Bifrost's model-list endpoint on `bifrost.fabriclab.ca`. The app tries `{base}/v1/models`, then `{origin}/v1/models`.
- Confirm the resource-provider list and diagnostic-settings check in the preflight script against current FortiCNAPP docs.
