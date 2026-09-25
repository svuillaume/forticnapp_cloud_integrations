"""Session state + agent loop (LLM <-> tools) over Bifrost."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import llm
from .config import settings
from .tools import run_tool, tool_specs

RUNTIME_ADDENDUM = f"""
## Runtime environment (this chat application)

- You are running inside a web chat window. The user sees your replies (Markdown) and a
  collapsible trace of every tool call.
- Azure access: when the `azure` MCP tools are available, YOU run Azure commands yourself
  through tool calls, as the identity signed in with `az login` on the agent host:
    * `az_cli` (read-only, allow-listed), e.g. ["account","show"], ["ad","signed-in-user","show"],
      ["account","list","--all"], ["role","assignment","list",...].
    * `azure_run_preflight` runs the full preflight and returns the JSON report. Prefer it for Phase 1.
    * `az_cli_write` (app/SP creation, role assignments, provider registration) and
      `azure_create_client_secret` (secret straight into Key Vault; you only get secret_ref "kv:<vault>/<name>")
      are write tools: the app shows the user a Confirm card before they run.
  If the tools are unavailable or az is not signed in, fall back to giving the user commands
  for Azure Cloud Shell and ask them to upload the preflight JSON ("Upload preflight report" button,
  read with `get_preflight_report`). The script is also downloadable from the header.
- FortiCNAPP tools come from the FortiCNAPP MCP server (forticnapp_* tools).
- Creation: call `forticnapp_create_azure_cloud_account`. The app intercepts every write tool:
  it validates with a dry run and shows the user a Confirm/Cancel card. Nothing is created until
  the user confirms. You will then receive a message starting with "[UI]" with the result.
- Client secrets: pass a reference, never a value. Either secret_ref="kv:<vault>/<name>" (from
  azure_create_client_secret) or "env:{settings.secret_env_prefix}<NAME>" held by the backend.
  Never ask the user to paste a secret.
- Keep replies short. End every turn with one clear question or next action.
"""


def system_prompt() -> str:
    skill = settings.skill_path.read_text() if settings.skill_path.is_file() else ""
    # Drop YAML front-matter
    if skill.startswith("---"):
        skill = skill.split("---", 2)[-1]
    return skill.strip() + "\n\n" + RUNTIME_ADDENDUM.strip()


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[dict] = field(default_factory=list)
    preflight: dict | None = None
    pending: dict[str, dict] = field(default_factory=dict)
    created: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.messages.append({"role": "system", "content": system_prompt()})

    def preflight_view(self, section: str = "summary", only_status: str | None = None) -> dict:
        r = self.preflight
        if not r:
            return {"status": "not_uploaded",
                    "message": "No preflight report uploaded yet. Ask the user to run the script in Azure Cloud Shell and upload the JSON."}
        checks = r.get("checks", [])
        if only_status:
            checks = [c for c in checks if c.get("status") == only_status]
        summary = {
            "tool": r.get("tool"), "version": r.get("version"), "timestamp": r.get("timestamp"),
            "status": r.get("status"), "tenant": r.get("tenant"), "consoleUser": r.get("consoleUser"),
            "subscriptions": r.get("subscriptions"), "principals": r.get("principals"),
            "missing": r.get("missing"),
            "counts": {s: sum(1 for c in r.get("checks", []) if c.get("status") == s)
                       for s in ("PASS", "WARN", "FAIL", "ERROR", "INFO")},
        }
        if section == "summary":
            return summary
        if section == "checks":
            return {"checks": checks}
        return {**summary, "checks": checks}


SESSIONS: dict[str, Session] = {}


def get_session(session_id: str | None) -> Session:
    if session_id and session_id in SESSIONS:
        return SESSIONS[session_id]
    s = Session()
    SESSIONS[s.id] = s
    return s


def _clip(obj: Any) -> str:
    text = json.dumps(obj, default=str)
    if len(text) > settings.tool_result_max_chars:
        text = text[: settings.tool_result_max_chars] + '..."[truncated]"'
    return text


async def run_turn(session: Session, model: str, user_text: str) -> dict:
    """Append the user message, loop LLM <-> tools, return reply + UI events."""
    session.messages.append({"role": "user", "content": user_text})
    events: list[dict] = []

    for _ in range(settings.max_agent_steps):
        msg = await llm.chat(model, session.messages, tool_specs())
        tool_calls = msg.get("tool_calls") or []
        assistant = {"role": "assistant", "content": msg.get("content") or ""}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        session.messages.append(assistant)

        if not tool_calls:
            return {"reply": assistant["content"], "events": events,
                    "pending": [a for a in session.pending.values() if a["status"] == "pending"]}

        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await run_tool(name, args, session)
            events.append({"type": "tool", "name": name, "args": args, "result": result})
            session.messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                                     "name": name, "content": _clip(result)})

    return {"reply": "_Stopped after the maximum number of tool steps. Ask me to continue._",
            "events": events,
            "pending": [a for a in session.pending.values() if a["status"] == "pending"]}
