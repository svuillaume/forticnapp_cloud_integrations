"""FastAPI entry point: chat UI + API.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm
from .agent import get_session, run_turn
from .config import ROOT, settings
from .forticnapp import client
from .mcp_hub import hub
from .tools import execute_action

@asynccontextmanager
async def lifespan(_: FastAPI):
    await hub.start()
    yield
    await hub.stop()


app = FastAPI(title="FortiCNAPP Azure Onboarding Agent", version="1.1.0", lifespan=lifespan)
STATIC = ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class ChatIn(BaseModel):
    session_id: str | None = None
    model: str | None = None
    message: str


class PreflightIn(BaseModel):
    session_id: str | None = None
    report: dict


class ActionIn(BaseModel):
    session_id: str
    model: str | None = None
    decision: str  # confirm | cancel


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def config() -> dict:
    return {
        "backend": settings.backend,
        "llm_api": settings.llm_api,
        "llm_base_url": settings.llm_base_url,
        "default_model": settings.default_model,
        "forticnapp_account": settings.lw_account or None,
        "forticnapp_configured": client.configured,
        "allow_create": settings.allow_create,
        "secret_env_prefix": settings.secret_env_prefix,
        "mcp_servers": list(hub.sessions),
        "mcp_tools": sorted(hub.tools),
        "mcp_errors": hub.errors,
    }


@app.get("/api/models")
async def models() -> dict:
    return await llm.list_models()


@app.post("/api/session")
async def new_session() -> dict:
    s = get_session(None)
    return {"session_id": s.id}


@app.post("/api/chat")
async def chat(body: ChatIn) -> dict:
    s = get_session(body.session_id)
    model = body.model or settings.default_model
    try:
        result = await run_turn(s, model, body.message)
    except RuntimeError as exc:
        # Roll back the unanswered user message so the conversation stays valid
        if s.messages and s.messages[-1]["role"] == "user":
            s.messages.pop()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"session_id": s.id, "model": model, **result}


@app.post("/api/preflight")
async def upload_preflight(body: PreflightIn) -> dict:
    s = get_session(body.session_id)
    r = body.report
    if r.get("tool") != "forticnapp-azure-preflight" or "checks" not in r:
        raise HTTPException(status_code=400, detail="Not a forticnapp-azure-preflight JSON report.")
    s.preflight = r
    return {"session_id": s.id, "status": r.get("status"), "tenant": r.get("tenant"),
            "subscriptions": len(r.get("subscriptions") or []), "missing": len(r.get("missing") or [])}


@app.post("/api/actions/{action_id}")
async def decide(action_id: str, body: ActionIn) -> dict:
    s = get_session(body.session_id)
    action = s.pending.get(action_id)
    if not action or action["status"] != "pending":
        raise HTTPException(status_code=404, detail="No pending action with that id.")
    if body.decision == "confirm":
        result = await execute_action(s, action_id)
        note = f"[UI] The user CONFIRMED action {action_id} ({action['tool']}). " \
               f"Result: {json.dumps(result)[:4000]}"
    else:
        action["status"] = "cancelled"
        result = {"cancelled": True}
        note = f"[UI] The user CANCELLED action {action_id}. Nothing was created."
    turn = await run_turn(s, body.model or settings.default_model, note)
    return {"session_id": s.id, "action": {"id": action_id, "status": action["status"], "result": result}, **turn}


PREFLIGHT_SCRIPT_URL = (
    "https://raw.githubusercontent.com/svuillaume/forticnapp_cloud_integrations/main/azure_preflight_check.sh"
)


@app.get("/download/preflight.sh")
async def download_preflight() -> RedirectResponse:
    # Canonical source of the preflight script now lives at
    # https://github.com/svuillaume/forticnapp_cloud_integrations — redirect there instead of
    # serving the bundled copy, so users always get the latest version.
    return RedirectResponse(PREFLIGHT_SCRIPT_URL)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})
