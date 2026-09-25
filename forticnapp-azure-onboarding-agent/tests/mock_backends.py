"""Mock Bifrost (Anthropic route) + mock FortiCNAPP API for local testing.

Run:  uvicorn tests.mock_backends:app --port 9000
Then: ANTHROPIC_BASE_URL=http://127.0.0.1:9000/anthropic LW_ACCOUNT=http://127.0.0.1:9000 ...
"""
from __future__ import annotations

import json
import uuid

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI()
TENANT = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
ACCOUNTS = [
    {"intgGuid": "AZ_CFG_1", "name": "azure-legacy-cfg", "type": "AzureCfg", "enabled": 1,
     "state": {"ok": True, "lastUpdatedTime": "2026-09-20T10:00:00Z"},
     "data": {"tenantId": "cccccccc-0000-0000-0000-dddddddddddd",
              "credentials": {"clientId": "x", "clientSecret": "SHOULD-BE-REDACTED"}}},
    {"intgGuid": "AWS_1", "name": "aws-prod", "type": "AwsCfg", "enabled": 1,
     "state": {"ok": True}, "data": {"awsAccountId": "123456789012"}},
]
CALLS: list[dict] = []   # record of LLM requests for assertions


# ------------------------------- FortiCNAPP ---------------------------------
def _auth(authorization: str | None) -> None:
    if authorization != "Bearer mock-token":
        raise HTTPException(401, "bad token")


@app.post("/api/v2/access/tokens")
async def token(req: Request, x_lw_uaks: str | None = Header(None)):
    body = await req.json()
    if x_lw_uaks != "lw-secret" or body.get("keyId") != "lw-key":
        raise HTTPException(401, "bad creds")
    return {"token": "mock-token", "expiresAt": "2099-01-01T00:00:00Z"}


@app.get("/api/v2/CloudAccounts")
async def list_all(authorization: str | None = Header(None)):
    _auth(authorization)
    return {"data": ACCOUNTS}


@app.get("/api/v2/schemas/CloudAccounts")
async def schema(authorization: str | None = Header(None)):
    _auth(authorization)
    return {"oneOf": [{"title": "AzureCfg", "required": ["tenantId", "credentials"]},
                      {"title": "AzureAlSeq", "required": ["tenantId", "credentials", "queueUrl"]}]}


@app.get("/api/v2/CloudAccounts/{ref}")
async def by_type_or_guid(ref: str, authorization: str | None = Header(None)):
    _auth(authorization)
    by_type = [a for a in ACCOUNTS if a["type"] == ref]
    if by_type or ref in ("AzureCfg", "AzureAlSeq", "AwsCfg"):
        return {"data": by_type}
    match = [a for a in ACCOUNTS if a["intgGuid"] == ref]
    if not match:
        raise HTTPException(404, "not found")
    return {"data": match[0]}


@app.post("/api/v2/CloudAccounts")
async def create(req: Request, authorization: str | None = Header(None)):
    _auth(authorization)
    body = await req.json()
    assert body["data"]["credentials"]["clientSecret"] == "real-azure-secret", "secret not resolved"
    new = {**body, "intgGuid": "AZ_NEW_" + uuid.uuid4().hex[:6], "state": {"ok": True}}
    ACCOUNTS.append(new)
    return {"data": new}


# ------------------------------- Bifrost (Anthropic route) ------------------
@app.get("/anthropic/v1/models")
async def models(authorization: str | None = Header(None)):
    if authorization != "Bearer sk-bf-test":
        raise HTTPException(401, "bad virtual key")
    return {"data": [{"id": "qwen3.8-27b-anthropic"}, {"id": "anthropic/claude-sonnet-5"}]}


def _validate(body: dict) -> None:
    msgs = body["messages"]
    assert msgs[0]["role"] == "user", "first message must be user"
    for a, b in zip(msgs, msgs[1:]):
        assert a["role"] != b["role"], "roles must alternate"
    open_ids = set()
    for m in msgs:
        for blk in m["content"]:
            if blk["type"] == "tool_use":
                open_ids.add(blk["id"])
            if blk["type"] == "tool_result":
                assert blk["tool_use_id"] in open_ids, "tool_result without tool_use"
    assert body["system"] and body["tools"] and body["max_tokens"] > 0


def _tool(name: str, args: dict) -> dict:
    return {"content": [{"type": "text", "text": ""},
                        {"type": "tool_use", "id": "tu_" + uuid.uuid4().hex[:8], "name": name, "input": args}],
            "stop_reason": "tool_use"}


def _text(t: str) -> dict:
    return {"content": [{"type": "text", "text": "<think>internal</think>" + t}], "stop_reason": "end_turn"}


@app.post("/anthropic/v1/messages")
async def messages(req: Request, authorization: str | None = Header(None)):
    if authorization != "Bearer sk-bf-test":
        raise HTTPException(401, "bad virtual key")
    body = await req.json()
    try:
        _validate(body)
    except AssertionError as exc:
        raise HTTPException(400, f"invalid anthropic request: {exc}")
    CALLS.append(body)
    last = body["messages"][-1]["content"]
    if last[0]["type"] == "tool_result":
        result = json.loads(last[0]["content"])
        if isinstance(result, dict) and result.get("status") == "awaiting_user_confirmation":
            return _text("I've prepared the integration. **Review the card and click Confirm** to create it.")
        return _text("Here is what I found:\n\n```json\n" + json.dumps(result, indent=1)[:600] + "\n```\n\nNext: upload your preflight report?")
    text = last[0].get("text", "")
    if "[UI]" in text:
        return _text("✅ Done. The integration was created. Next I'll verify its state.")
    low = text.lower()
    if "azure user" in low:
        return _tool("az_cli", {"args": ["ad", "signed-in-user", "show"]})
    if "run preflight" in low:
        return _tool("azure_run_preflight", {})
    if "not allowed" in low:
        return _tool("az_cli", {"args": ["account", "get-access-token"]})
    if "create app" in low:
        return _tool("az_cli_write", {"args": ["ad", "app", "create", "--display-name", "forticnapp-azure-lab"]})
    if "create secret" in low:
        return _tool("azure_create_client_secret", {"app_id": "99999999-8888-7777-6666-555555555555", "key_vault": "kv-lab"})
    if "integration with kv" in low:
        return _tool("forticnapp_create_azure_cloud_account", {
            "name": "azure-kv-cfg", "type": "AzureCfg", "tenant_id": "abababab-1111-2222-3333-cdcdcdcdcdcd",
            "client_id": "99999999-8888-7777-6666-555555555555",
            "secret_ref": "kv:kv-lab/forticnapp-azure-client-secret"})
    if "preflight" in text.lower():
        return _tool("get_preflight_report", {"section": "summary"})
    if "create" in text.lower():
        return _tool("forticnapp_create_azure_cloud_account", {
            "name": "azure-contoso-cfg", "type": "AzureCfg", "tenant_id": TENANT,
            "client_id": "11111111-2222-3333-4444-555555555555", "secret_ref": "env:FCNAPP_SECRET_AZURE"})
    if "list" in text.lower():
        return _tool("forticnapp_list_cloud_accounts", {})
    return _text("Hello! Ready to start your Azure onboarding.")


@app.get("/_calls")
async def calls():
    return {"count": len(CALLS)}


@app.get("/_leak")
async def leak():
    """Did any secret ever reach the LLM?"""
    text = json.dumps(CALLS)
    return {"llm_requests": len(CALLS),
            "azure_secret_seen": "real-azure-secret" in text,
            "stored_secret_seen": "SHOULD-BE-REDACTED" in text,
            "lw_secret_seen": "lw-secret" in text}


@app.get("/_trace")
async def trace(last: int = 2):
    """Shape of the last N LLM requests: what the model received and what it was asked."""
    out = []
    for body in CALLS[-last:]:
        m = body["messages"][-1]
        out.append([{"type": b["type"], **({"name": b["name"]} if "name" in b else {}),
                     **({"text": b["text"][:60]} if b["type"] == "text" else {}),
                     **({"tool_result": b["content"][:120]} if b["type"] == "tool_result" else {})}
                    for b in m["content"]])
    return out
