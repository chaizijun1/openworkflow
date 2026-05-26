"""MCP (Model Context Protocol) client — connect the whole MCP tool ecosystem at once.

Instead of re-implementing WebFetch/DB/SaaS tools one by one, this connects to any MCP servers
the user configures and exposes *their* tools to subagents. One integration → filesystem,
GitHub, Slack, Postgres, browser, and every other MCP server.

Tools are namespaced ``mcp__<server>__<tool>`` (matching Claude Code's MCP tool naming), so they
compose with the built-in ToolBox without collisions.

Config: ``~/.openworkflow/mcp.json`` (Claude-compatible shape)::

    {"mcpServers": {
       "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]},
       "github":     {"command": "uvx", "args": ["mcp-server-github"], "env": {"GITHUB_TOKEN": "..."}},
       "remote":     {"url": "https://mcp.example.com/sse"}
    }}

Requires the optional ``mcp`` SDK (``pip install 'openworkflow[mcp]'``). If the SDK or config is
absent, the manager contributes no tools — a graceful no-op, never an error.
"""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any


def default_mcp_config_path() -> Path:
    return Path(os.path.expanduser("~")) / ".openworkflow" / "mcp.json"


def load_mcp_config(path: str | Path | None = None) -> dict[str, dict]:
    p = Path(path) if path else default_mcp_config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("mcpServers", data) if isinstance(data, dict) else {}


class MCPManager:
    """Connects configured MCP servers and exposes their tools to the subagent.

    Lifecycle: ``await connect()`` once per run (idempotent), then ``tool_specs()`` /
    ``execute()``; ``await aclose()`` at the end. Designed to fail soft: a server that won't
    start is logged-and-skipped, not fatal.
    """

    def __init__(self, config: dict[str, dict] | None = None) -> None:
        self.config = config if config is not None else load_mcp_config()
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, Any] = {}            # server -> ClientSession
        self._tools: dict[str, tuple[str, str]] = {}   # mcp__srv__tool -> (server, raw_name)
        self._specs: list[dict] = []
        self._connected = False
        self.errors: list[str] = []

    def configured(self) -> bool:
        return bool(self.config)

    async def connect(self) -> None:
        if self._connected or not self.config:
            self._connected = True
            return
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            self.errors.append("mcp SDK not installed (pip install 'openworkflow[mcp]')")
            self._connected = True
            return

        self._stack = AsyncExitStack()
        for server, cfg in self.config.items():
            try:
                if "command" in cfg:
                    params = StdioServerParameters(
                        command=cfg["command"], args=cfg.get("args", []),
                        env={**os.environ, **cfg.get("env", {})},
                    )
                    read, write = await self._stack.enter_async_context(stdio_client(params))
                elif "url" in cfg:
                    from mcp.client.sse import sse_client
                    read, write = await self._stack.enter_async_context(sse_client(cfg["url"]))
                else:
                    self.errors.append(f"{server}: config needs 'command' or 'url'")
                    continue
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                self._sessions[server] = session
                for tool in listed.tools:
                    name = f"mcp__{server}__{tool.name}"
                    self._tools[name] = (server, tool.name)
                    self._specs.append({
                        "name": name,
                        "description": tool.description or f"{server}.{tool.name}",
                        "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
                    })
            except Exception as e:  # noqa: BLE001 - fail soft per server
                self.errors.append(f"{server}: {type(e).__name__}: {e}")
        self._connected = True

    def tool_specs(self) -> list[dict]:
        return list(self._specs)

    def handles(self, name: str) -> bool:
        return name in self._tools

    async def execute(self, name: str, tool_input: dict) -> str:
        if name not in self._tools:
            return f"error: unknown MCP tool '{name}'"
        server, raw = self._tools[name]
        session = self._sessions.get(server)
        if session is None:
            return f"error: MCP server '{server}' not connected"
        try:
            result = await session.call_tool(raw, tool_input)
        except Exception as e:  # noqa: BLE001
            return f"error: {type(e).__name__}: {e}"
        # MCP returns content blocks; concatenate text parts
        parts = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            parts.append(text if text is not None else str(block))
        return "\n".join(parts) if parts else "(no content)"

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self._sessions.clear()
        self._connected = False
