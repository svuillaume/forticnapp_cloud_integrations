# Option A: Claude Desktop as the MCP client

In this setup, Claude Desktop is the chat window, the agent and the model. It connects to the bundled
**FortiCNAPP MCP server** (`mcp_server/forticnapp_mcp.py`) over stdio.

```
Claude Desktop (chat + agent + Claude model) ──stdio/MCP──► forticnapp MCP server ──HTTPS──► FortiCNAPP API v2
```

> In this mode the model is whichever Claude model you pick in Claude Desktop. It does not go through Bifrost.
> To use Bifrost models (e.g. `qwen3.8-27b-anthropic`), use Option B, the web chat app (see README).

## 1. Install once

```bash
cd forticnapp-azure-onboarding-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                              # then edit .env
```

In `.env`, set at least:

```bash
account = "demo"                  # -> demo.lacework.net
api_key = "FORTINET_XXXXXXX"
api_secret = "<FortiCNAPP API secret>"
version = 2
FCNAPP_SECRET_AZURE=<Azure app client secret>    # referenced as secret_ref=env:FCNAPP_SECRET_AZURE
```

The MCP server reads `.env` from the project folder, so none of these secrets go in the Claude Desktop config.

## 2. Register the server in Claude Desktop

Open **Claude Desktop → Settings → Developer → Edit Config**. The file is in one of these places:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add the entry below and replace `/ABS/PATH` with the project's absolute path:

```json
{
  "mcpServers": {
    "forticnapp": {
      "command": "/ABS/PATH/forticnapp-azure-onboarding-agent/.venv/bin/python",
      "args": ["-m", "mcp_server.forticnapp_mcp"],
      "env": { "PYTHONPATH": "/ABS/PATH/forticnapp-azure-onboarding-agent" }
    },
    "azure": {
      "command": "/ABS/PATH/forticnapp-azure-onboarding-agent/.venv/bin/python",
      "args": ["-m", "mcp_server.azure_mcp"],
      "env": { "PYTHONPATH": "/ABS/PATH/forticnapp-azure-onboarding-agent" }
    }
  }
}
```

On Windows, the command is `C:\\ABS\\PATH\\forticnapp-azure-onboarding-agent\\.venv\\Scripts\\python.exe`.

Restart Claude Desktop. The tools menu should then list **forticnapp** (6 tools) and **azure** (4 tools). Run `az login` on the same machine first; the Azure tools act as that identity.

## 3. Give Claude the onboarding instructions

Add `SKILL.md` as a skill, or paste it into a Project's instructions. Then:

1. Say *"Start a new FortiCNAPP Azure integration."*
2. Run `scripts/forticnapp-azure-preflight.sh` in Azure Cloud Shell and **attach the JSON report** to the chat.
3. Claude checks existing integrations with `forticnapp_find_azure_integrations`.
4. For creation, Claude calls `forticnapp_create_azure_cloud_account`:
   - first with `dry_run=true`, which returns a redacted preview;
   - then with `dry_run=false` once you say yes.

## Safety in this mode

- **Tool approval:** Claude Desktop asks you to approve tool calls. Keep "Always allow" **off** for `forticnapp_create_azure_cloud_account`, so every creation needs your click.
- **Dry run by default:** the create tool defaults to `dry_run=true`. Validation, the duplicate check (same tenant and type) and the redacted preview happen before anything is written.
- **Secret references only:** secrets are passed as references (`env:FCNAPP_SECRET_*`). The server refuses any other environment variable, including `LW_API_SECRET`.
- **Hard off switch:** set `ALLOW_CREATE=false` in `.env` to make the server read-only.

## Test without Claude Desktop

```bash
npx @modelcontextprotocol/inspector .venv/bin/python -m mcp_server.forticnapp_mcp
```
