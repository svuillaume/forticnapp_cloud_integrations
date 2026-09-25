"""Bifrost LLM gateway client.

Supports both Bifrost routes:
  - Anthropic Messages API  (LLM_API=anthropic, base e.g. https://bifrost.example/anthropic)
  - OpenAI Chat Completions (LLM_API=openai,    base e.g. https://bifrost.example)

Conversation state is kept in OpenAI format internally (system/user/assistant/tool
messages with tool_calls); it is converted to Anthropic content blocks on the way out
and the response is converted back.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import settings


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if settings.llm_api == "anthropic":
        h["anthropic-version"] = "2023-06-01"
    if settings.llm_api_key:
        h["Authorization"] = f"Bearer {settings.llm_api_key}"
        h["x-bf-vk"] = settings.llm_api_key          # Bifrost virtual key header
        if settings.llm_api == "anthropic":
            h["x-api-key"] = settings.llm_api_key
    return h


# ---------------------------------------------------------------------------
# Model list
# ---------------------------------------------------------------------------
async def list_models() -> dict[str, Any]:
    """Return {"models": [...], "source": "bifrost"|"fallback", "error"?}."""
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(settings.llm_base_url))
    urls = list(dict.fromkeys([f"{settings.llm_base_url}/v1/models", f"{origin}/v1/models"]))
    last_err = None
    async with httpx.AsyncClient(timeout=20) as http:
        for url in urls:
            try:
                resp = await http.get(url, headers=_headers())
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code} on {url}"
                    continue
                body = resp.json()
                items = body.get("data", body) if isinstance(body, dict) else body
                ids = sorted({(m.get("id") if isinstance(m, dict) else str(m)) for m in items or []} - {None, ""})
                if settings.model_filter:
                    rx = re.compile(settings.model_filter)
                    ids = [m for m in ids if rx.search(m)]
                if ids:
                    if settings.default_model and settings.default_model not in ids:
                        ids.insert(0, settings.default_model)
                    return {"models": ids, "source": "bifrost"}
                last_err = f"empty model list from {url}"
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
    return {"models": settings.fallback_models, "source": "fallback", "error": last_err}


# ---------------------------------------------------------------------------
# OpenAI <-> Anthropic conversion
# ---------------------------------------------------------------------------
def _to_anthropic(messages: list[dict], tools: list[dict]) -> dict:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "user":
            out.append({"role": "user", "content": [{"type": "text", "text": m["content"] or "(empty)"}]})
        elif role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": args})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no content)"}]})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            # Consecutive tool results go into one user turn
            if out and out[-1]["role"] == "user" and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    # Merge any accidental consecutive same-role turns (Anthropic requires alternation)
    merged: list[dict] = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"].extend(m["content"])
        else:
            merged.append(m)
    return {
        "system": system,
        "messages": merged,
        "tools": [{"name": t["function"]["name"], "description": t["function"]["description"],
                   "input_schema": t["function"]["parameters"]} for t in tools],
    }


def _from_anthropic(body: dict) -> dict:
    text, calls = [], []
    for block in body.get("content", []):
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block["id"], "type": "function",
                          "function": {"name": block["name"], "arguments": json.dumps(block.get("input", {}))}})
    msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(t for t in text if t).strip()}
    if calls:
        msg["tool_calls"] = calls
    return msg


def _strip_think(msg: dict) -> dict:
    """Some open models (e.g. Qwen) emit <think>...</think>; hide it from the user."""
    if msg.get("content"):
        msg["content"] = re.sub(r"<think>.*?</think>", "", msg["content"], flags=re.S).strip()
    return msg


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
async def chat(model: str, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
    """One model call. Returns an OpenAI-style assistant message (content + tool_calls)."""
    if settings.llm_api == "anthropic":
        url = f"{settings.llm_base_url}/v1/messages"
        payload = {"model": model, "max_tokens": settings.llm_max_tokens,
                   "temperature": settings.llm_temperature, **_to_anthropic(messages, tools)}
    else:
        url = f"{settings.llm_base_url}/v1/chat/completions"
        payload = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto",
                   "temperature": settings.llm_temperature, "max_tokens": settings.llm_max_tokens}

    async with httpx.AsyncClient(timeout=settings.llm_timeout) as http:
        resp = await http.post(url, headers=_headers(), json=payload)
    if resp.status_code >= 400:
        raise RuntimeError(f"Bifrost HTTP {resp.status_code} ({url}): {resp.text[:500]}")
    body = resp.json()
    try:
        if settings.llm_api == "anthropic":
            return _strip_think(_from_anthropic(body))
        return _strip_think(body["choices"][0]["message"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Bifrost response: {str(body)[:500]}") from exc
