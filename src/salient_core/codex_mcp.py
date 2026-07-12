from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import select
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final

from .runtime import AgentTool, JsonValue, ToolBundle

_PROTOCOL_VERSION: Final = "2025-03-26"
# Ceiling on a single tool handler. Enforced on BOTH sides: the HTTP thread's
# poll deadline bounds the client-visible response, and an asyncio.wait_for on
# the owner loop bounds the coroutine itself — so a handler that ignores
# cancellation (or whose HTTP thread has already given up) still can't run
# unbounded. Mirrored into codex_config's tool_timeout_sec so the Codex client
# and the gateway agree on the budget.
_TOOL_TIMEOUT_SEC: Final = 120
# Blocking delegation tools (`ask_agent` / `ask_agents` / `ask_operator` / …) exist
# to WAIT — for a child's reply, a swarm fan-out, or an operator answer — and
# manage their own caller-side wait internally (bus `_compute_ask_agent_timeout`,
# capped ~4h). Bounding them at the default 120s made codex agents give up on any
# swarm or deep delegation after two minutes (the children keep running; the caller
# just stops listening). Give these a ceiling ABOVE the bus cap so the bus's own
# timeout — with its proper "did not reply within wait window" error — always fires
# first. Non-blocking tools keep the tight 120s bound.
_BLOCKING_TOOL_TIMEOUT_SEC: Final = 4 * 3600 + 300  # 4h + slop


def _tool_timeout(bare_name: str) -> int:
    return _BLOCKING_TOOL_TIMEOUT_SEC if bare_name.startswith("ask_") else _TOOL_TIMEOUT_SEC


_SHARED_GATEWAY: CodexMcpGateway | None = None


def _json_data(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _json_data(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_json_data(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class McpCredential:
    owner: str
    token: str
    url: str
    bearer_token_env_var: str

    def codex_config(self) -> dict[str, JsonValue]:
        return {
            "url": self.url,
            "bearer_token_env_var": self.bearer_token_env_var,
            "required": True,
            "startup_timeout_sec": 10,
            # The codex CLI's own per-tool wait. Must cover the longest tool the
            # gateway will run (a blocking ask_* delegation) — the gateway still
            # bounds every INDIVIDUAL tool per `_tool_timeout`, so a non-blocking
            # tool that wedges is answered (with an error) at 120s regardless.
            "tool_timeout_sec": _BLOCKING_TOOL_TIMEOUT_SEC,
        }


@dataclass(slots=True)
class _Catalog:
    owner: str
    tools: ToolBundle
    loop: asyncio.AbstractEventLoop
    pending: set[Future[JsonValue]]
    revoked: bool = False


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, sock: socket.socket, gateway: CodexMcpGateway) -> None:
        self.gateway = gateway
        super().__init__(sock.getsockname(), _Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()
        self.server_activate()


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self._write(404, {"error": "not found"})
            return
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        catalog = self.server.gateway._catalog(token)
        if catalog is None:
            self._write(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._write(400, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._write(400, {"error": "invalid JSON-RPC request"})
            return
        status, result = self.server.gateway._dispatch(catalog, payload, self._disconnected)
        self._write(status, result)

    def _disconnected(self) -> bool:
        readable, _, _ = select.select((self.connection,), (), (), 0)
        if not readable:
            return False
        try:
            disconnected = self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
            return bool(disconnected)
        except (BlockingIOError, ConnectionResetError, OSError):
            return True

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _write(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            return


class CodexMcpGateway:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._catalogs: dict[str, _Catalog] = {}
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._url: str | None = None

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("MCP gateway is not running")
        return self._url

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self._server is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind(("127.0.0.1", 0))
        sock.listen(socket.SOMAXCONN)
        server = _Server(sock, self)
        port = int(server.server_address[1])
        self._server = server
        self._url = f"http://127.0.0.1:{port}/mcp"
        thread = threading.Thread(
            target=server.serve_forever,
            name="salient-codex-mcp",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def issue(
        self,
        owner: str,
        tools: ToolBundle,
        *,
        bearer_token_env_var: str = "SALIENT_CODEX_MCP_TOKEN",
    ) -> McpCredential:
        if self._server is None:
            self.start()
        token = secrets.token_urlsafe(32)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._catalogs[token] = _Catalog(owner, tools, loop, set())
        return McpCredential(owner, token, self.url, bearer_token_env_var)

    def revoke(self, token: str) -> None:
        catalog: _Catalog | None = None
        with self._lock:
            for stored in tuple(self._catalogs):
                if hmac.compare_digest(stored, token):
                    catalog = self._catalogs.pop(stored)
                    # Flip under the same lock _dispatch must take to schedule a
                    # tool call, so a concurrent dispatch either sees revoked and
                    # bails, or is already in `pending` and gets cancelled below.
                    catalog.revoked = True
                    break
            stop = not self._catalogs
        if catalog is not None:
            for pending in tuple(catalog.pending):
                pending.cancel()
        if stop:
            self._stop_async()

    def _stop_async(self) -> None:
        server = self._server
        self._server = None
        self._url = None
        thread = self._thread
        self._thread = None
        if server is None:
            return

        def stop() -> None:
            server.shutdown()
            server.server_close()
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)

        threading.Thread(target=stop, name="salient-codex-mcp-stop", daemon=True).start()

    def close(self) -> None:
        server = self._server
        self._server = None
        self._url = None
        with self._lock:
            catalogs = tuple(self._catalogs.values())
            self._catalogs.clear()
        for catalog in catalogs:
            for pending in tuple(catalog.pending):
                pending.cancel()
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2)

    def _catalog(self, token: str) -> _Catalog | None:
        with self._lock:
            for stored, catalog in self._catalogs.items():
                if hmac.compare_digest(stored, token):
                    return catalog
        return None

    def _dispatch(
        self,
        catalog: _Catalog,
        request: dict[str, Any],
        disconnected: Callable[[], bool] = lambda: False,
    ) -> tuple[int, dict[str, Any]]:
        request_id = request.get("id")
        method = request.get("method")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "salient", "version": "1"},
            }
            return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}
        if method == "notifications/initialized":
            return 202, {}
        if method == "ping":
            return 200, {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            tools = [self._tool_schema(tool) for tool in catalog.tools.tools]
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": tools},
            }
        if method != "tools/call":
            return 404, self._error(request_id, -32601, "method not found")
        params = request.get("params")
        if not isinstance(params, dict):
            return 400, self._error(request_id, -32602, "invalid params")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return 400, self._error(request_id, -32602, "invalid params")
        tool = next(
            (candidate for candidate in catalog.tools.tools if candidate.name == name), None
        )
        if tool is None:
            # Codex presents/forwards MCP tools under a server-qualified name
            # (e.g. "salient__list_agents"), and salient's per-agent prompts are
            # written in the Claude wire form "mcp__bus__<alias>__<tool>", which
            # the model may echo verbatim. Bus bare names are single snake_case
            # tokens (no "__"), so the last "__"-delimited segment resolves any
            # of those forms unambiguously — the same dual-namespace tolerance
            # the Claude SDK path already has (bus tools registered on both the
            # `mcp__<alias>__` and `mcp__bus__<alias>__` servers).
            bare = name.rsplit("__", 1)[-1]
            if bare != name:
                tool = next(
                    (candidate for candidate in catalog.tools.tools if candidate.name == bare),
                    None,
                )
        if tool is None:
            return 404, self._error(request_id, -32602, f"unknown tool: {name!r}")

        # Blocking delegation tools (ask_*) manage their own long wait; everything
        # else stays on the tight 120s bound.
        tool_timeout = _tool_timeout(tool.name)

        async def invoke() -> JsonValue:
            # Loop-side deadline: bounds the coroutine even if the HTTP thread
            # has already returned/died or the handler ignores cancel().
            return await asyncio.wait_for(tool.handler(arguments), tool_timeout)

        # Schedule the handler and register the future in ONE critical section,
        # gated on the revoked flag. run_coroutine_threadsafe only does a
        # non-blocking call_soon_threadsafe, so holding _lock across it is safe
        # and closes the revoke-vs-dispatch race: revoke() sets `revoked` and
        # snapshots `pending` under this same lock, so every future is either
        # seen-and-cancelled or never scheduled.
        with self._lock:
            if catalog.revoked:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "agent revoked"}],
                        "isError": True,
                    },
                }
            future: Future[JsonValue] = asyncio.run_coroutine_threadsafe(invoke(), catalog.loop)
            catalog.pending.add(future)
        try:
            # Thread-side deadline: bounds the client-visible response. Break on
            # future.done() so a handler that itself raises TimeoutError isn't
            # confused with a poll timeout (in 3.11+ asyncio.TimeoutError,
            # concurrent.futures.TimeoutError and builtins.TimeoutError are the
            # same type). Give the loop-side wait_for a small grace so its clean
            # cancellation lands first.
            deadline = time.monotonic() + tool_timeout + 1
            while not future.done():
                if disconnected() or time.monotonic() >= deadline:
                    future.cancel()
                    break
                try:
                    future.result(timeout=0.1)
                except FutureTimeoutError:
                    pass
            tool_result = future.result()  # value, or re-raises the handler error
            text = json.dumps(tool_result, separators=(",", ":"))
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except (CancelledError, FutureTimeoutError, RuntimeError, ValueError, OSError) as error:
            future.cancel()
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": str(error)}],
                    "isError": True,
                },
            }
        finally:
            with self._lock:
                catalog.pending.discard(future)

    @staticmethod
    def _tool_schema(tool: AgentTool) -> dict[str, Any]:
        schema = _json_data(tool.input_schema)
        result: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": schema,
        }
        if tool.annotations:
            result["annotations"] = _json_data(tool.annotations)
        return result

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def translate_external_mcp(config: dict[str, JsonValue]) -> dict[str, JsonValue]:
    transport = config.get("type", "stdio")
    if transport == "stdio":
        command = config.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("stdio MCP server requires command")
        result = {key: value for key, value in config.items() if key != "type"}
        return result
    if transport == "http":
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("HTTP MCP server requires url")
        return {key: value for key, value in config.items() if key != "type"}
    raise ValueError(f"unsupported Codex MCP transport: {transport}")


def get_codex_mcp_gateway() -> CodexMcpGateway:
    global _SHARED_GATEWAY
    if _SHARED_GATEWAY is None:
        _SHARED_GATEWAY = CodexMcpGateway()
    return _SHARED_GATEWAY
