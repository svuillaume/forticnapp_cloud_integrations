"""Agent-side tool router.

- Local tool: get_preflight_report (reads the JSON the user uploaded in this chat).
- Every other tool comes from MCP servers (see mcp_hub.py).
- Write gate: any MCP tool NOT annotated readOnlyHint=true is never executed on the
  model's say-so. The router first runs it with dry_run=true when the tool supports it
  (validation + duplicate check + redacted preview), stores it as a *pending action*,
  and the UI shows a Confirm/Cancel card. Only a human Confirm executes it.
"""
from __future__ import annotations

import uuid
from typing import Any

from .mcp_hub import hub
from .ops import redact

LOCAL_TOOLS: list[dict] = [{
    "type": "function",
    "function": {
        "name": "get_preflight_report",
        "description": "Read the Azure preflight JSON report the user uploaded in this chat (from forticnapp-azure-preflight.sh): status, tenant, subscriptions, principals, missing items, checks.",
        "parameters": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["summary", "checks", "all"]},
                "only_status": {"type": "string", "enum": ["FAIL", "WARN", "ERROR", "PASS", "INFO"]},
            },
        },
    },
}]


def tool_specs() -> list[dict]:
    return LOCAL_TOOLS + hub.specs()


async def run_tool(name: str, args: dict, session: Any) -> Any:
    if name == "get_preflight_report":
        return session.preflight_view(args.get("section", "summary"), args.get("only_status"))

    tool = hub.tools.get(name)
    if not tool:
        return {"error": f"unknown tool {name}"}
    if tool.read_only:
        return redact(await hub.call(name, args))

    # ---- write gate ----------------------------------------------------------
    supports_dry_run = "dry_run" in (tool.schema.get("properties") or {})
    preview: Any = redact({k: v for k, v in args.items() if k != "dry_run"})
    if supports_dry_run:
        check = redact(await hub.call(name, {**args, "dry_run": True}))
        if not (isinstance(check, dict) and check.get("status") == "dry_run_ok"):
            return check  # invalid / duplicate / error -> back to the model, nothing pending
        preview = check.get("preview", preview)
    action_id = uuid.uuid4().hex[:12]
    session.pending[action_id] = {"id": action_id, "tool": name, "args": args,
                                  "preview": preview, "status": "pending"}
    return {"status": "awaiting_user_confirmation", "action_id": action_id, "preview": preview,
            "instruction": "Tell the user to review the confirmation card and click Confirm or Cancel. Do not claim it was done."}


async def execute_action(session: Any, action_id: str) -> Any:
    action = session.pending.get(action_id)
    if not action or action["status"] != "pending":
        return {"error": "No pending action with that id"}
    tool = hub.tools[action["tool"]]
    args = dict(action["args"])
    if "dry_run" in (tool.schema.get("properties") or {}):
        args["dry_run"] = False
    result = redact(await hub.call(action["tool"], args))
    failed = isinstance(result, dict) and (result.get("error") or result.get("status") in ("error", "invalid", "blocked_duplicate", "disabled"))
    action["status"] = "failed" if failed else "done"
    return result
