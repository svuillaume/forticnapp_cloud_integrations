"""FortiCNAPP MCP server: CloudAccounts tools over the FortiCNAPP (Lacework) API v2.

Transports:
  stdio (default, spawned by the agent):  python -m mcp_server.forticnapp_mcp
  streamable HTTP (shared service):       python -m mcp_server.forticnapp_mcp --http --port 8765

Env: LW_ACCOUNT, LW_API_KEY, LW_API_SECRET, [LW_SUBACCOUNT],
     FCNAPP_SECRET_<NAME> (Azure client secrets), ALLOW_CREATE=true|false
"""
from __future__ import annotations

import argparse
import logging

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app import ops
from app.forticnapp import FortiCNAPPError

logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log API URLs per request
mcp = FastMCP("forticnapp", log_level="WARNING")
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


async def _safe(coro):
    try:
        return await coro
    except FortiCNAPPError as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=READ)
async def forticnapp_list_cloud_accounts() -> dict:
    """List all cloud account integrations in the FortiCNAPP tenant (name, type, intgGuid, enabled, state, tenant/account id)."""
    return await _safe(ops.list_cloud_accounts())


@mcp.tool(annotations=READ)
async def forticnapp_list_cloud_accounts_by_type(type: str) -> dict:
    """List FortiCNAPP cloud account integrations of one type, e.g. AzureCfg (Azure configuration) or AzureAlSeq (Azure Activity Log)."""
    return await _safe(ops.list_cloud_accounts_by_type(type))


@mcp.tool(annotations=READ)
async def forticnapp_get_cloud_account(intgGuid: str) -> dict:
    """Get one FortiCNAPP cloud account integration by intgGuid, including state/health. Use it to verify a new integration."""
    return await _safe(ops.get_cloud_account(intgGuid))


@mcp.tool(annotations=READ)
async def forticnapp_find_azure_integrations(tenant_id: str) -> dict:
    """Find existing Azure integrations (AzureCfg + AzureAlSeq) for an Azure tenant GUID. Call before any creation."""
    return await _safe(ops.find_azure_integrations(tenant_id))


@mcp.tool(annotations=READ)
async def forticnapp_get_cloud_account_schema(type: str = "") -> dict:
    """Fetch the live CloudAccounts JSON schema from the FortiCNAPP API (optionally for one type, e.g. AzureCfg)."""
    res = await _safe(ops.get_cloud_account_schema(type or None))
    return res if isinstance(res, dict) else {"schema": res}


@mcp.tool(annotations=WRITE)
async def forticnapp_create_azure_cloud_account(
    name: str, type: str, tenant_id: str, client_id: str, secret_ref: str,
    queue_url: str = "", dry_run: bool = True,
) -> dict:
    """Create a FortiCNAPP Azure cloud account (type AzureCfg or AzureAlSeq).

    secret_ref: reference to the Azure client secret held by this server, format env:FCNAPP_SECRET_<NAME>.
    Never pass a secret value. queue_url is required for AzureAlSeq.
    dry_run=true (default) validates, checks for duplicates and returns a redacted preview without creating.
    """
    return await _safe(ops.create_azure_cloud_account(
        name, type, tenant_id, client_id, secret_ref, queue_url or None, dry_run))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    if a.http:
        mcp.settings.host, mcp.settings.port = a.host, a.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
