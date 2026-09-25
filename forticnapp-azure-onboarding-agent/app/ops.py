"""FortiCNAPP CloudAccounts operations (used by the MCP server).

- Every result is redacted (keys like secret/password/token are masked).
- Creation validates input, blocks duplicates for the same Azure tenant + type,
  and resolves the client secret server-side from a reference (env:FCNAPP_SECRET_<NAME>).
  The secret value is never returned.
"""
from __future__ import annotations

import os
import re
from typing import Any

from .config import settings
from .forticnapp import FortiCNAPPError, client

GUID_RX = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Some small/local models fabricate a syntactically-valid but meaningless GUID (usually all
# zeros) instead of asking the user for the real tenant_id. Reject the obvious placeholder so
# the tool result tells the model to ask, rather than silently answering for a fake tenant.
PLACEHOLDER_GUID_RX = re.compile(r"^0{8}-0{4}-0{4}-0{4}-0{12}$")


def _is_placeholder_guid(value: str) -> bool:
    return bool(PLACEHOLDER_GUID_RX.match(value or ""))
SECRET_KEY_RX = re.compile(r"secret|password|token|private_?key", re.I)
QUEUE_RX = re.compile(r"^https://[a-z0-9]{3,24}\.queue\.core\.windows\.net/[a-z0-9-]{3,63}/?$")
AZURE_TYPES = ("AzureCfg", "AzureAlSeq")


REF_KEY_RX = re.compile(r"(ref|_id|Id|_name|Name|Hint|hint|keyId|type|Type)$")


def _is_secret_key(k: str) -> bool:
    return bool(SECRET_KEY_RX.search(k)) and not REF_KEY_RX.search(k)


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if _is_secret_key(k) and isinstance(v, str) and v else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def summarize(accounts: Any) -> Any:
    if not isinstance(accounts, dict) or "data" not in accounts:
        return redact(accounts)
    items = accounts["data"] if isinstance(accounts["data"], list) else [accounts["data"]]
    out = []
    for a in items:
        data = a.get("data", {}) or {}
        state = a.get("state", {}) or {}
        out.append({
            "name": a.get("name"), "type": a.get("type"), "intgGuid": a.get("intgGuid"),
            "enabled": a.get("enabled"), "stateOk": state.get("ok"),
            "lastUpdated": state.get("lastUpdatedTime") or a.get("createdOrUpdatedTime"),
            "tenantId": data.get("tenantId"),
            "accountId": data.get("awsAccountId") or data.get("projectId") or data.get("subscriptionId"),
        })
    return {"count": len(out), "accounts": out}


def resolve_secret(ref: str) -> str:
    """env:FCNAPP_SECRET_<NAME>  or  kv:<vault>/<secret-name> (read with the host's az login)."""
    if ref.startswith("kv:"):
        m = re.match(r"^kv:([A-Za-z0-9-]{3,24})/([A-Za-z0-9-]{1,127})$", ref)
        if not m:
            raise ValueError("secret_ref must look like kv:<vault>/<secret-name>")
        import subprocess
        p = subprocess.run([os.getenv("AZ_BIN", "az"), "keyvault", "secret", "show", "--vault-name", m[1],
                            "--name", m[2], "--query", "value", "-o", "tsv"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0 or not p.stdout.strip():
            raise ValueError(f"cannot read Key Vault secret {m[1]}/{m[2]} (check az login / Key Vault access)")
        return p.stdout.strip()
    if not ref.startswith("env:"):
        raise ValueError("secret_ref must look like env:<NAME> or kv:<vault>/<name>")
    name = ref[4:]
    if not name.startswith(settings.secret_env_prefix):
        raise ValueError(f"secret_ref env var must start with {settings.secret_env_prefix}")
    value = os.getenv(name, "")
    if not value:
        raise ValueError(f"environment variable {name} is not set on the MCP server")
    return value


def build_payload(name: str, type_: str, tenant_id: str, client_id: str, secret: str,
                  queue_url: str | None) -> dict:
    data: dict[str, Any] = {"tenantId": tenant_id, "credentials": {"clientId": client_id, "clientSecret": secret}}
    if type_ == "AzureAlSeq":
        data["queueUrl"] = (queue_url or "").rstrip("/")
    return {"name": name, "type": type_, "enabled": 1, "data": data}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
async def list_cloud_accounts() -> dict:
    return summarize(await client.list_cloud_accounts())


async def list_cloud_accounts_by_type(type_: str) -> dict:
    if not re.match(r"^[A-Za-z0-9]{3,40}$", type_):
        return {"error": "invalid type"}
    return summarize(await client.list_cloud_accounts_by_type(type_))


async def get_cloud_account(intg_guid: str) -> dict:
    if not re.match(r"^[A-Za-z0-9_\-]{8,80}$", intg_guid):
        return {"error": "invalid intgGuid"}
    return redact(await client.get_cloud_account(intg_guid))


async def find_azure_integrations(tenant_id: str) -> dict:
    if not GUID_RX.match(tenant_id):
        return {"error": "tenant_id must be a GUID"}
    if _is_placeholder_guid(tenant_id):
        return {"error": "tenant_id looks like a placeholder (all-zeros). "
                          "Ask the user for their real Azure tenant ID before calling this tool."}
    found = []
    for t in AZURE_TYPES:
        res = await client.list_cloud_accounts_by_type(t)
        if isinstance(res, dict) and res.get("error"):
            return redact(res)
        found += [a for a in summarize(res).get("accounts", [])
                  if str(a.get("tenantId", "")).lower() == tenant_id.lower()]
    return {"tenant_id": tenant_id, "count": len(found), "integrations": found}


async def get_cloud_account_schema(type_: str | None = None) -> Any:
    schema = await client.get_cloud_account_schema()
    if not type_ or not isinstance(schema, dict):
        return schema
    candidates = schema.get("oneOf") or schema.get("anyOf") or schema.get("data") or []
    if isinstance(candidates, list):
        match = [c for c in candidates if type_ in str(c)]
        if match:
            return {"type": type_, "schema": match}
    return schema


async def create_azure_cloud_account(name: str, type_: str, tenant_id: str, client_id: str,
                                     secret_ref: str, queue_url: str | None, dry_run: bool) -> dict:
    errors = []
    if type_ not in AZURE_TYPES:
        errors.append(f"type must be one of {list(AZURE_TYPES)}")
    if not GUID_RX.match(tenant_id or ""):
        errors.append("tenant_id must be a GUID")
    elif _is_placeholder_guid(tenant_id):
        errors.append("tenant_id looks like a placeholder (all-zeros); ask the user for the real Azure tenant ID")
    if not GUID_RX.match(client_id or ""):
        errors.append("client_id must be a GUID")
    elif _is_placeholder_guid(client_id):
        errors.append("client_id looks like a placeholder (all-zeros); ask the user for the real app/client ID")
    if not 3 <= len(name or "") <= 128:
        errors.append("name must be 3-128 characters")
    if type_ == "AzureAlSeq" and not QUEUE_RX.match(queue_url or ""):
        errors.append("queue_url must be https://<storage>.queue.core.windows.net/<queue>")
    try:
        resolve_secret(secret_ref)
    except ValueError as exc:
        errors.append(f"secret_ref: {exc}")
    if errors:
        return {"status": "invalid", "errors": errors}

    existing = await find_azure_integrations(tenant_id)
    if existing.get("error"):
        return {"status": "error", "detail": existing}
    dupes = [a for a in existing["integrations"] if a.get("type") == type_]
    if dupes:
        return {"status": "blocked_duplicate",
                "message": f"An {type_} integration already exists for tenant {tenant_id}.",
                "existing": dupes}

    preview = build_payload(name, type_, tenant_id, client_id, "***", queue_url)
    if dry_run:
        return {"status": "dry_run_ok", "preview": preview}
    if not settings.allow_create:
        return {"status": "disabled", "message": "ALLOW_CREATE=false on the MCP server"}
    try:
        result = await client.create_cloud_account(
            build_payload(name, type_, tenant_id, client_id, resolve_secret(secret_ref), queue_url))
    except FortiCNAPPError as exc:
        return {"status": "error", "detail": str(exc)}
    if result.get("error"):
        return {"status": "error", "detail": redact(result)}
    return {"status": "created", "result": redact(result)}
