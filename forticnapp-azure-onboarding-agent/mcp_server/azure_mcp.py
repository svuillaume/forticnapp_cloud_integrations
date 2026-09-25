"""Azure MCP server: lets the agent run Azure CLI commands and the preflight script.

The agent runs `az` on the host where this server runs, as whoever is signed in there
(`az login` beforehand, ideally the deployer's own delegated login, not a standing Owner SP).

Tools
  az_cli                      read-only, allow-listed `az` commands (no shell, -o json)
  azure_run_preflight         runs scripts/forticnapp-azure-preflight.sh, returns the JSON report
  az_cli_write                allow-listed write commands, dry_run=true by default (gated by the app)
  azure_create_client_secret  creates an app secret and stores it in Key Vault; returns only
                              a reference (kv:<vault>/<name>), never the value

Transports: stdio (default) or --http --port 8766
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.ops import redact

ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT = ROOT / "scripts" / "forticnapp-azure-preflight.sh"
AZ = os.getenv("AZ_BIN", "az")
TIMEOUT = int(os.getenv("AZ_TIMEOUT", "120"))

logging.getLogger("httpx").setLevel(logging.WARNING)
mcp = FastMCP("azure", log_level="WARNING")
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

# Allow-lists: leading words of the az command
READ_ALLOW = [
    ("version",), ("account", "show"), ("account", "list"),
    ("account", "management-group", "list"), ("account", "management-group", "show"),
    ("ad", "signed-in-user", "show"), ("ad", "signed-in-user", "list-owned-objects"),
    ("ad", "user", "show"), ("ad", "sp", "show"), ("ad", "sp", "list"),
    ("ad", "app", "show"), ("ad", "app", "list"),
    ("ad", "app", "credential", "list"), ("ad", "app", "federated-credential", "list"),
    ("ad", "app", "permission", "list"),
    ("role", "assignment", "list"), ("role", "definition", "list"),
    ("provider", "list"), ("provider", "show"),
    ("monitor", "diagnostic-settings", "subscription", "list"),
    ("group", "list"), ("keyvault", "list"), ("storage", "account", "list"),
    ("rest",),  # GET only, Graph / ARM only (checked below)
]
WRITE_ALLOW = [
    ("ad", "app", "create"), ("ad", "sp", "create"),
    ("ad", "app", "permission", "add"), ("ad", "app", "permission", "admin-consent"),
    ("role", "assignment", "create"), ("role", "assignment", "delete"),
    ("provider", "register"),
]
REST_HOSTS = ("https://graph.microsoft.com/", "https://management.azure.com/")
SAFE_ARG = re.compile(r"^[^\n\r\x00]{1,2000}$")
NAME_RX = re.compile(r"^[A-Za-z0-9-]{1,127}$")


def _match(args: list[str], allow: list[tuple]) -> bool:
    return any(tuple(args[: len(p)]) == p for p in allow)


def _check(args: list[str], allow: list[tuple]) -> str | None:
    if not args or not all(isinstance(a, str) and SAFE_ARG.match(a) for a in args):
        return "invalid arguments"
    if args[0] == "az":
        args.pop(0)
    if not _match(args, allow):
        return f"command 'az {' '.join(args[:4])}' is not allowed by this tool"
    if args[0] == "rest":
        method = "get"
        for i, a in enumerate(args):
            if a in ("--method", "-m") and i + 1 < len(args):
                method = args[i + 1].lower()
        url = next((args[i + 1] for i, a in enumerate(args) if a in ("--url", "--uri", "-u") and i + 1 < len(args)), "")
        if method != "get" or not url.startswith(REST_HOSTS):
            return "az rest is limited to GET on graph.microsoft.com / management.azure.com"
    if any(a in ("--debug",) for a in args):
        return "--debug is not allowed"
    return None


def _run(args: list[str], stdin: str | None = None, timeout: int = TIMEOUT) -> dict:
    if not shutil.which(AZ):
        return {"error": "Azure CLI (az) not found on the agent host"}
    if "-o" not in args and "--output" not in args:
        args = [*args, "-o", "json"]
    try:
        p = subprocess.run([AZ, *args], input=stdin, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "AZURE_CORE_NO_COLOR": "1", "AZURE_CORE_ONLY_SHOW_ERRORS": "1"})
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s"}
    out: object
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        out = p.stdout[-8000:]
    res = {"exitCode": p.returncode, "output": redact(out)}
    if p.returncode != 0:
        res["stderr"] = p.stderr[-2000:]
        if "az login" in p.stderr:
            res["hint"] = "The agent host is not signed in to Azure. Run 'az login' on the host running the Azure MCP server."
    return res


@mcp.tool(annotations=READ)
async def az_cli(args: list[str]) -> dict:
    """Run a READ-ONLY Azure CLI command and return its JSON output.

    args: the az arguments as a list, without 'az'. Examples:
      ["account", "show"]                      -> signed-in user, tenant, current subscription
      ["account", "list", "--all"]             -> subscriptions
      ["ad", "signed-in-user", "show"]         -> Entra user details
      ["role", "assignment", "list", "--assignee", "<objectId>", "--all", "--include-inherited"]
      ["ad", "sp", "list", "--display-name", "lacework"]
      ["rest", "--method", "GET", "--url", "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.directoryRole"]
    Only allow-listed read commands are accepted.
    """
    args = list(args)
    err = _check(args, READ_ALLOW)
    return {"error": err, "allowed": [" ".join(p) for p in READ_ALLOW]} if err else _run(args)


@mcp.tool(annotations=READ)
async def azure_run_preflight(subscriptions: str = "", app_id: str = "") -> dict:
    """Run the FortiCNAPP Azure preflight (read-only) as the signed-in az identity and return its JSON report.

    subscriptions: optional comma-separated subscription IDs (default: all visible).
    app_id: optional FortiCNAPP SP appId to evaluate instead of discovering it.
    Returns status, tenant, subscriptions, principals, missing items and non-PASS checks.
    """
    if not PREFLIGHT.is_file():
        return {"error": "preflight script not found"}
    env = {**os.environ, "NO_COLOR": "1"}
    if subscriptions:
        if not re.match(r"^[0-9a-fA-F,\- ]+$", subscriptions):
            return {"error": "invalid subscriptions"}
        env["FORTICNAPP_SUBSCRIPTIONS"] = subscriptions
    if app_id:
        if not re.match(r"^[0-9a-fA-F\-]{36}$", app_id):
            return {"error": "invalid app_id"}
        env["FORTICNAPP_APP_ID"] = app_id
    with tempfile.TemporaryDirectory() as d:
        try:
            p = subprocess.run(["bash", str(PREFLIGHT)], cwd=d, env=env, capture_output=True,
                               text=True, timeout=int(os.getenv("PREFLIGHT_TIMEOUT", "900")))
        except subprocess.TimeoutExpired:
            return {"error": "preflight timed out"}
        reports = sorted(Path(d).glob("forticnapp-azure-preflight-*.json"))
        if not reports:
            return {"error": "no report produced", "exitCode": p.returncode, "tail": p.stdout[-3000:]}
        r = json.loads(reports[-1].read_text())
    checks = r.pop("checks", [])
    r["counts"] = {s: sum(1 for c in checks if c["status"] == s) for s in ("PASS", "WARN", "FAIL", "ERROR", "INFO")}
    r["issues"] = [c for c in checks if c["status"] in ("FAIL", "WARN", "ERROR")]
    r["exitCode"] = p.returncode
    return redact(r)


@mcp.tool(annotations=WRITE)
async def az_cli_write(args: list[str], dry_run: bool = True) -> dict:
    """Run an allow-listed Azure CLI WRITE command (app/SP creation, role assignment, provider registration).

    dry_run=true (default) only validates and returns the exact command. The chat app asks the
    user to confirm before it runs with dry_run=false.
    Allowed: az ad app create | ad sp create | ad app permission add/admin-consent |
             role assignment create/delete | provider register
    Client secrets are NOT created here; use azure_create_client_secret.
    """
    args = list(args)
    err = _check(args, WRITE_ALLOW)
    if err:
        return {"status": "invalid", "error": err, "allowed": [" ".join(p) for p in WRITE_ALLOW]}
    if dry_run:
        return {"status": "dry_run_ok", "preview": {"command": "az " + " ".join(args)}}
    res = _run(args)
    return {"status": "done" if res.get("exitCode") == 0 else "error", **res}


@mcp.tool(annotations=WRITE)
async def azure_create_client_secret(app_id: str, key_vault: str, secret_name: str = "forticnapp-azure-client-secret",
                                     years: int = 1, dry_run: bool = True) -> dict:
    """Create a client secret for an app registration and store it directly in Azure Key Vault.

    The secret value is never returned. The result contains secret_ref="kv:<vault>/<name>", which
    the FortiCNAPP MCP server resolves when creating the cloud account.
    dry_run=true (default) only validates.
    """
    if not re.match(r"^[0-9a-fA-F\-]{36}$", app_id or ""):
        return {"status": "invalid", "error": "app_id must be a GUID"}
    if not NAME_RX.match(key_vault or "") or not NAME_RX.match(secret_name or ""):
        return {"status": "invalid", "error": "invalid key_vault or secret_name"}
    if not 1 <= int(years) <= 2:
        return {"status": "invalid", "error": "years must be 1 or 2"}
    ref = f"kv:{key_vault}/{secret_name}"
    preview = {"app_id": app_id, "key_vault": key_vault, "secret_name": secret_name, "years": years,
               "commands": [f"az ad app credential reset --id {app_id} --append --display-name forticnapp --years {years}",
                            f"az keyvault secret set --vault-name {key_vault} --name {secret_name} --file <tmp, 0600, deleted>"]}
    if dry_run:
        return {"status": "dry_run_ok", "preview": preview}

    res = _run(["ad", "app", "credential", "reset", "--id", app_id, "--append", "--display-name", "forticnapp",
                "--years", str(years), "--query", "password", "-o", "tsv"])
    # _run redacts JSON only; tsv output is the raw secret -> take it without echoing anywhere
    if res.get("exitCode") != 0:
        return {"status": "error", "stderr": res.get("stderr")}
    secret = str(res.get("output") or "").strip()
    res = None
    if not secret:
        return {"status": "error", "error": "no secret returned by az"}
    fd, path = tempfile.mkstemp()
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret)
        kv = _run(["keyvault", "secret", "set", "--vault-name", key_vault, "--name", secret_name,
                   "--file", path, "--query", "id"])
    finally:
        secret = None
        os.unlink(path)
    if kv.get("exitCode") != 0:
        return {"status": "error", "stage": "keyvault", "stderr": kv.get("stderr"),
                "note": "A secret was added to the app but not stored. Remove it with 'az ad app credential delete'."}
    return {"status": "created", "secret_ref": ref, "key_vault_secret_id": kv.get("output")}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--http", action="store_true")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    a = p.parse_args()
    if a.http:
        mcp.settings.host, mcp.settings.port = a.host, a.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
