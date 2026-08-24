"""MCP (Model Context Protocol) client support.

liteagent acts as an MCP *client*: it connects to one or more external MCP
*servers* (configured in ``data/mcp_servers.json``), asks each one what tools
it exposes, and merges those into the same OpenAI-style function-calling
schema used for the built-in tools. When the model calls one of those tools,
the call is forwarded over MCP to the owning server and the result is routed
back — from the model's point of view there is no difference between a
built-in tool and an MCP tool.

The official MCP Python SDK is async-native and keeps a long-lived
stdio-connected subprocess per server. The rest of this app (FastAPI routes,
the Agent loop) is synchronous, so this module runs one dedicated background
thread with its own asyncio event loop for the lifetime of the process, and
exposes a small synchronous facade (``list_servers`` / ``schema`` /
``call_tool`` / ``add_server`` / ``remove_server`` / ``reconnect``) that
bridges into it via ``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re
import threading

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_NAME_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _tool_id(server: str, tool: str) -> str:
    raw = f"mcp__{server}__{tool}"
    safe = _NAME_SANITIZE_RE.sub("_", raw)
    return safe[:64]


_MAX_STRUCTURED_STRING_CHARS = 2000


def _sanitize_structured(value: Any, _depth: int = 0) -> Any:
    """Recursively replace any oversized string inside an MCP structuredContent
    payload with a short placeholder, so accidental binary/base64 blobs never
    reach the LLM context (see call_tool's docstring-comment for why)."""
    if _depth > 20:  # defensive cap against pathological/cyclic structures
        return value
    if isinstance(value, str):
        if len(value) > _MAX_STRUCTURED_STRING_CHARS:
            return f"[omitted: {len(value)} chars — too large to forward to the LLM as text]"
        return value
    if isinstance(value, dict):
        return {k: _sanitize_structured(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_structured(v, _depth + 1) for v in value]
    return value


DEFAULT_CONFIG = [
    {
        "name": "filesystem-demo",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "{workspace}/mcp_demo"],
        "env": {},
        "enabled": True,
    }
]


def seed_default_config(config_path: Path, workspace: Path) -> None:
    """Write a starter config (one well-known public MCP server) the first time
    this app runs, so the mechanism has something real to show/verify. Never
    overwrites an existing file, so user edits are always preserved."""
    (workspace / "mcp_demo").mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


class MCPBridge:
    def __init__(self, config_path: Path, workspace: Path):
        self.config_path = config_path
        self.workspace = workspace
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}  # name -> {status, error, tools}
        self._sessions: dict[str, ClientSession] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._tool_index: dict[str, tuple[str, str]] = {}  # tool_id -> (server, original tool name)

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run_loop, name="mcp-bridge", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        for name, cfg in self._load_config().items():
            self._state[name] = {"status": "disabled" if not cfg.get("enabled", True) else "connecting", "error": None, "tools": []}
            if cfg.get("enabled", True):
                self._spawn(name, cfg)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    # ---- config persistence --------------------------------------------

    def _load_config(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {item["name"]: item for item in raw if isinstance(item, dict) and item.get("name")}

    def _save_config(self, servers: dict[str, dict[str, Any]]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(list(servers.values()), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---- connecting / disconnecting a single server ---------------------

    def _spawn(self, name: str, cfg: dict[str, Any]) -> None:
        assert self._loop is not None
        # Fire-and-forget: the coroutine manages its own lifecycle (updates
        # self._state / self._sessions on connect, cleans up in `finally`),
        # so there is no separate task handle to track from the calling thread.
        asyncio.run_coroutine_threadsafe(self._run_server(name, cfg), self._loop)

    def _resolve_args(self, args: list[str]) -> list[str]:
        return [str(a).replace("{workspace}", str(self.workspace)) for a in args]

    async def _run_server(self, name: str, cfg: dict[str, Any]) -> None:
        with self._lock:
            self._state[name] = {"status": "connecting", "error": None, "tools": []}
        stop_event = asyncio.Event()
        self._stop_events[name] = stop_event
        try:
            params = StdioServerParameters(
                command=cfg["command"],
                args=self._resolve_args(cfg.get("args") or []),
                env=(cfg.get("env") or None),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=20)
                    result = await asyncio.wait_for(session.list_tools(), timeout=20)
                    tools = [
                        {
                            "id": _tool_id(name, t.name),
                            "name": t.name,
                            "description": t.description or "",
                            "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                        }
                        for t in result.tools
                    ]
                    with self._lock:
                        self._sessions[name] = session
                        self._state[name] = {"status": "connected", "error": None, "tools": tools}
                        for tool in tools:
                            self._tool_index[tool["id"]] = (name, tool["name"])
                    await stop_event.wait()
        except Exception as exc:
            with self._lock:
                self._state[name] = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "tools": []}
        finally:
            with self._lock:
                self._sessions.pop(name, None)
                for tool_id in [k for k, v in self._tool_index.items() if v[0] == name]:
                    self._tool_index.pop(tool_id, None)

    async def _disconnect(self, name: str) -> None:
        """Signal the server's run loop to stop, then wait for its cleanup to finish."""
        stop_event = self._stop_events.pop(name, None)
        if stop_event:
            stop_event.set()
        for _ in range(100):  # up to ~10s
            with self._lock:
                still_running = name in self._sessions
            if not still_running:
                return
            await asyncio.sleep(0.1)

    # ---- synchronous facade --------------------------------------------

    def list_servers(self) -> list[dict[str, Any]]:
        config = self._load_config()
        with self._lock:
            state_snapshot = {k: dict(v) for k, v in self._state.items()}
        items = []
        for name, cfg in config.items():
            state = state_snapshot.get(name, {"status": "disabled" if not cfg.get("enabled", True) else "pending", "error": None, "tools": []})
            items.append({
                "name": name,
                "command": cfg.get("command"),
                "args": cfg.get("args") or [],
                "enabled": cfg.get("enabled", True),
                "status": state["status"],
                "error": state.get("error"),
                "tools": [{"name": t["name"], "description": t["description"]} for t in state.get("tools", [])],
            })
        return items

    def schema(self) -> list[dict[str, Any]]:
        with self._lock:
            all_tools: list[dict[str, Any]] = []
            for state in self._state.values():
                all_tools.extend(state.get("tools", []))
        return [
            {
                "type": "function",
                "function": {
                    "name": t["id"],
                    "description": t["description"] or t["name"],
                    "parameters": t["input_schema"] or {"type": "object", "properties": {}},
                },
            }
            for t in all_tools
        ]

    def has_tool(self, tool_id: str) -> bool:
        with self._lock:
            return tool_id in self._tool_index

    def call_tool(self, tool_id: str, arguments: dict[str, Any], timeout: float = 60.0) -> Any:
        with self._lock:
            target = self._tool_index.get(tool_id)
            session = self._sessions.get(target[0]) if target else None
        if not target or not session:
            raise RuntimeError(f"MCP tool not connected: {tool_id}")
        _, original_name = target
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(session.call_tool(original_name, arguments), self._loop)
        result = future.result(timeout=timeout)
        if getattr(result, "isError", False):
            raise RuntimeError("; ".join(getattr(c, "text", str(c)) for c in result.content))
        parts = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
                continue
            data = getattr(block, "data", None)
            if data is not None:
                # Binary content (image/audio/etc.): never dump the raw base64
                # payload into the LLM context — a single screenshot can be
                # hundreds of KB of base64 text and blow up the request,
                # causing the backend LLM server to fail (e.g. HTTP 500).
                mime = getattr(block, "mimeType", None) or "unknown"
                btype = getattr(block, "type", None) or block.__class__.__name__
                parts.append(
                    f"[{btype} content omitted: mimeType={mime}, "
                    f"base64 length={len(data)} chars — binary content is not "
                    f"forwarded to the LLM as text]"
                )
                continue
            parts.append(str(block))
        # structuredContent is a separate MCP-protocol field some servers use to
        # duplicate the *same* payload in a machine-readable shape (e.g.
        # read_media_file returns the full base64 image again here even though
        # the `data` block above was already omitted) -- without sanitizing it
        # too, that binary payload sneaks straight back into the LLM context via
        # this field and can blow past a backend's request-size/context limit
        # (seen in practice as an opaque HTTP 400 from OpenRouter). Recursively
        # strip any oversized string value so structuredContent stays usable for
        # genuinely small structured data without re-leaking large binary blobs.
        structured = _sanitize_structured(getattr(result, "structuredContent", None))
        return {"content": parts, "structured": structured}

    def add_server(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None, enabled: bool = True) -> None:
        servers = self._load_config()
        servers[name] = {"name": name, "command": command, "args": args, "env": env or {}, "enabled": enabled}
        self._save_config(servers)
        with self._lock:
            self._state[name] = {"status": "connecting" if enabled else "disabled", "error": None, "tools": []}
        if enabled and self._loop is not None:
            self._spawn(name, servers[name])

    def remove_server(self, name: str) -> None:
        servers = self._load_config()
        servers.pop(name, None)
        self._save_config(servers)
        with self._lock:
            self._state.pop(name, None)
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._disconnect(name), self._loop)

    def reconnect(self, name: str) -> None:
        servers = self._load_config()
        cfg = servers.get(name)
        if not cfg:
            raise KeyError(name)
        if self._loop is not None:
            future = asyncio.run_coroutine_threadsafe(self._disconnect(name), self._loop)
            future.result(timeout=15)
            if cfg.get("enabled", True):
                self._spawn(name, cfg)
