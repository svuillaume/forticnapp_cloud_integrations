"""MCP client hub: connects the agent to one or more MCP servers.

MCP_SERVERS (JSON, same shape as Claude Desktop's "mcpServers"):
  {"forticnapp": {"command": "python", "args": ["-m", "mcp_server.forticnapp_mcp"]}}
  {"forticnapp": {"url": "http://mcp-host:8765/mcp"}}          # streamable HTTP
Default: the bundled FortiCNAPP + Azure MCP servers over stdio (ENABLE_AZURE_TOOLS=false drops Azure).
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .config import ROOT


@dataclass
class RemoteTool:
    server: str
    name: str
    description: str
    schema: dict
    read_only: bool


class MCPHub:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, RemoteTool] = {}
        self.errors: dict[str, str] = {}

    @staticmethod
    def _config() -> dict[str, dict]:
        raw = os.getenv("MCP_SERVERS", "").strip()
        if raw:
            cfg = json.loads(raw)
            return cfg.get("mcpServers", cfg)
        servers = {"forticnapp": {"command": sys.executable, "args": ["-m", "mcp_server.forticnapp_mcp"]}}
        if os.getenv("ENABLE_AZURE_TOOLS", "true").lower() == "true":
            servers["azure"] = {"command": sys.executable, "args": ["-m", "mcp_server.azure_mcp"]}
        return servers

    async def start(self) -> None:
        for name, spec in self._config().items():
            try:
                if "url" in spec:
                    r, w, _ = await self._stack.enter_async_context(
                        streamablehttp_client(spec["url"], headers=spec.get("headers")))
                else:
                    params = StdioServerParameters(
                        command=spec["command"], args=spec.get("args", []),
                        env={**os.environ, **spec.get("env", {})}, cwd=spec.get("cwd", str(ROOT)))
                    r, w = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(r, w))
                await session.initialize()
                self.sessions[name] = session
                for t in (await session.list_tools()).tools:
                    ann = t.annotations
                    read_only = bool(ann and ann.readOnlyHint)
                    key = t.name if t.name not in self.tools else f"{name}__{t.name}"
                    self.tools[key] = RemoteTool(name, t.name, t.description or "", t.inputSchema, read_only)
            except Exception as exc:  # keep the app up even if one server fails
                self.errors[name] = f"{type(exc).__name__}: {exc}"

    async def stop(self) -> None:
        await self._stack.aclose()

    def specs(self) -> list[dict]:
        """Tool specs in OpenAI function format (converted to Anthropic format by llm.py)."""
        return [{"type": "function", "function": {"name": k, "description": t.description,
                                                  "parameters": t.schema or {"type": "object", "properties": {}}}}
                for k, t in self.tools.items()]

    async def call(self, key: str, args: dict) -> Any:
        t = self.tools.get(key)
        if not t:
            return {"error": f"unknown tool {key}"}
        res = await self.sessions[t.server].call_tool(t.name, args)
        if res.structuredContent is not None:
            out = res.structuredContent
            return out["result"] if isinstance(out, dict) and set(out) == {"result"} else out
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text, "isError": res.isError}


hub = MCPHub()
