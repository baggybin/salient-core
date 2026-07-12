from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request

from salient_core.runtime import AgentTool, ToolBundle


async def _echo(arguments):
    return {"echo": arguments.get("text", "")}


def _post(url: str, token: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_gateway_auth_isolation_and_revoke() -> None:
    from salient_core.codex_mcp import CodexMcpGateway

    bundle = ToolBundle((AgentTool("echo", "echo text", {"type": "object"}, _echo),))

    async def scenario() -> None:
        gateway = CodexMcpGateway()
        gateway.start()
        first = gateway.issue("agent-a", bundle)
        second = gateway.issue("agent-b", ToolBundle())
        try:
            status, listed = await asyncio.to_thread(
                _post,
                gateway.url,
                first.token,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert status == 200
            assert [tool["name"] for tool in listed["result"]["tools"]] == ["echo"]

            status, called = await asyncio.to_thread(
                _post,
                gateway.url,
                first.token,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "ok"}},
                },
            )
            assert status == 200
            assert json.loads(called["result"]["content"][0]["text"]) == {"echo": "ok"}

            status, _ = await asyncio.to_thread(
                _post,
                gateway.url,
                second.token,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {}},
                },
            )
            assert status == 404

            gateway.revoke(first.token)
            status, _ = await asyncio.to_thread(
                _post,
                gateway.url,
                first.token,
                {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
            )
            assert status == 401
        finally:
            gateway.close()

    asyncio.run(scenario())


def test_blocking_delegation_tools_get_a_long_gateway_timeout() -> None:
    # ask_* tools block waiting for a child/swarm/operator and manage their own
    # long caller-side wait; the gateway must not cancel them at the default 120s
    # (that made codex agents give up on swarms after 2 min). Non-blocking tools
    # keep the tight bound.
    from salient_core.codex_mcp import (
        _BLOCKING_TOOL_TIMEOUT_SEC,
        _TOOL_TIMEOUT_SEC,
        McpCredential,
        _tool_timeout,
    )

    assert _BLOCKING_TOOL_TIMEOUT_SEC > _TOOL_TIMEOUT_SEC
    for blocking in ("ask_agent", "ask_agents", "ask_operator", "ask_consensus"):
        assert _tool_timeout(blocking) == _BLOCKING_TOOL_TIMEOUT_SEC
    for quick in ("context_read", "list_agents", "scanner_scan", "context_write"):
        assert _tool_timeout(quick) == _TOOL_TIMEOUT_SEC

    # The codex CLI's own per-tool wait must cover the longest gateway tool.
    cfg = McpCredential("owner", "tok", "http://x/mcp", "ENV").codex_config()
    assert cfg["tool_timeout_sec"] == _BLOCKING_TOOL_TIMEOUT_SEC


def test_gateway_resolves_server_qualified_tool_name() -> None:
    # Codex forwards MCP tools under a server-qualified name, and salient's
    # per-agent prompts use the Claude wire form "mcp__bus__<alias>__<tool>"
    # which the model may echo verbatim. The gateway must resolve any such form
    # to the bare tool name (its last "__"-delimited segment) — otherwise every
    # codex bus tool call fails "unknown tool" while text turns work.
    from salient_core.codex_mcp import CodexMcpGateway

    bundle = ToolBundle((AgentTool("list_agents", "list", {"type": "object"}, _echo),))

    async def scenario() -> None:
        gateway = CodexMcpGateway()
        gateway.start()
        cred = gateway.issue("manager", bundle)
        try:
            for call_name in (
                "list_agents",  # bare — always worked
                "mcp__bus__manager__list_agents",  # Claude wire form from the prompt
                "salient__list_agents",  # codex server-qualified form
            ):
                status, called = await asyncio.to_thread(
                    _post,
                    gateway.url,
                    cred.token,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": call_name, "arguments": {"text": "ok"}},
                    },
                )
                assert status == 200, call_name
                assert called["result"]["isError"] is False, call_name
                assert json.loads(called["result"]["content"][0]["text"]) == {"echo": "ok"}

            # A genuinely unknown tool still 404s — and the error names it.
            status, miss = await asyncio.to_thread(
                _post,
                gateway.url,
                cred.token,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "nope__does_not_exist", "arguments": {}},
                },
            )
            assert status == 404
            assert "does_not_exist" in miss["error"]["message"]
        finally:
            gateway.close()

    asyncio.run(scenario())


def test_external_mcp_translation_rejects_legacy_sse() -> None:
    from salient_core.codex_mcp import translate_external_mcp

    assert translate_external_mcp({"type": "stdio", "command": "server", "args": ["--safe"]}) == {
        "command": "server",
        "args": ["--safe"],
    }
    assert translate_external_mcp({"type": "http", "url": "https://mcp.invalid"}) == {
        "url": "https://mcp.invalid"
    }
    try:
        translate_external_mcp({"type": "sse", "url": "https://mcp.invalid"})
    except ValueError as error:
        assert "unsupported Codex MCP transport" in str(error)
    else:
        raise AssertionError("legacy SSE transport was accepted")


def test_gateway_runs_handler_on_owner_loop_and_cancels_on_revoke() -> None:
    from salient_core.codex_mcp import CodexMcpGateway

    async def scenario() -> None:
        owner_thread = threading.get_ident()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow(_arguments):
            assert threading.get_ident() == owner_thread
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        gateway = CodexMcpGateway()
        gateway.start()
        credential = gateway.issue("owner", ToolBundle((AgentTool("slow", "", {}, slow),)))
        request = asyncio.create_task(
            asyncio.to_thread(
                _post,
                gateway.url,
                credential.token,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "slow", "arguments": {}},
                },
            )
        )
        await asyncio.wait_for(started.wait(), 1)
        gateway.revoke(credential.token)
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(request, 1)
        assert not gateway.running

    asyncio.run(scenario())


def test_gateway_dispatch_after_revoke_does_not_run_handler() -> None:
    # The revoke-vs-dispatch TOCTOU: a tools/call that reaches _dispatch after
    # revoke() has flipped the catalog's `revoked` flag must return isError and
    # never schedule the handler onto the owner loop.
    from salient_core.codex_mcp import CodexMcpGateway

    ran = threading.Event()

    async def handler(_arguments):
        ran.set()
        return {"ok": True}

    async def scenario() -> None:
        gateway = CodexMcpGateway()
        gateway.start()
        credential = gateway.issue(
            "owner", ToolBundle((AgentTool("t", "", {"type": "object"}, handler),))
        )
        catalog = gateway._catalog(credential.token)
        assert catalog is not None
        gateway.revoke(credential.token)  # flips catalog.revoked under _lock
        try:
            status, result = gateway._dispatch(
                catalog,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "t", "arguments": {}},
                },
            )
            assert status == 200
            assert result["result"]["isError"] is True
            await asyncio.sleep(0.05)
            assert not ran.is_set()
        finally:
            gateway.close()

    asyncio.run(scenario())


def test_gateway_bounds_runaway_handler_with_deadline(monkeypatch) -> None:
    # A handler that never completes must be cancelled and returned as isError
    # once the loop-side wait_for deadline fires, even though nothing revoked it.
    from salient_core import codex_mcp
    from salient_core.codex_mcp import CodexMcpGateway

    monkeypatch.setattr(codex_mcp, "_TOOL_TIMEOUT_SEC", 0.3)

    async def scenario() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def runaway(_arguments):
            started.set()
            try:
                await asyncio.Event().wait()  # never set
            except asyncio.CancelledError:
                cancelled.set()
                raise

        gateway = CodexMcpGateway()
        gateway.start()
        credential = gateway.issue("owner", ToolBundle((AgentTool("slow", "", {}, runaway),)))
        try:
            status, result = await asyncio.wait_for(
                asyncio.to_thread(
                    _post,
                    gateway.url,
                    credential.token,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "slow", "arguments": {}},
                    },
                ),
                5,
            )
            assert status == 200
            assert result["result"]["isError"] is True
            await asyncio.wait_for(cancelled.wait(), 1)
        finally:
            gateway.close()

    asyncio.run(scenario())
