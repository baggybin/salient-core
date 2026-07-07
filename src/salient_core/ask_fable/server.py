"""The ask_fable MCP server: gate a question, route it to Fable, return JSON.

Modeled on graphify's low-level ``mcp.server.Server`` usage. Two tools:
``ask`` (guarded reasoning, multi-turn) and ``reset_session`` (dump + clear).
Every response is a single ``TextContent`` carrying a JSON payload so callers
across harnesses get a stable, parseable result.
"""

from __future__ import annotations

import json
import time

import mcp.types as types
from mcp.server import Server

from . import audit, fable, guard
from .prompts import ASK_TOOL_DESCRIPTION
from .sessions import SessionStore

_ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "A specific question about concrete software code/architecture "
            "(structure, functionality, data flow, module/function relationships, routing).",
        },
        "context": {
            "type": "string",
            "default": "",
            "description": "Optional code snippets, file paths, or structural context.",
        },
        "session": {
            "type": "string",
            "default": "default",
            "description": "Conversation key. Reuse it to ask follow-ups (Fable keeps "
            "context); use a new key or reset=true to start a fresh topic.",
        },
        "reset": {
            "type": "boolean",
            "default": False,
            "description": "Dump+clear this session before asking, starting a fresh conversation.",
        },
    },
    "required": ["question"],
    "additionalProperties": False,
}

_RESET_SCHEMA = {
    "type": "object",
    "properties": {
        "session": {"type": "string", "default": "default", "description": "Session key to clear."},
        "save": {
            "type": "boolean",
            "default": True,
            "description": "Write the transcript to a file before clearing.",
        },
    },
    "required": [],
    "additionalProperties": False,
}


def _text(payload: dict) -> types.TextContent:
    return types.TextContent(type="text", text=json.dumps(payload))


def build_server() -> Server:
    server: Server = Server("ask_fable")
    store = SessionStore()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name="ask", description=ASK_TOOL_DESCRIPTION, inputSchema=_ASK_SCHEMA),
            types.Tool(
                name="reset_session",
                description="Dump (optionally to a file) and clear a Fable conversation session, "
                "so the next `ask` on that key starts a fresh topic.",
                inputSchema=_RESET_SCHEMA,
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        try:
            if name == "ask":
                return [_text(await _handle_ask(store, arguments or {}))]
            if name == "reset_session":
                return [_text(_handle_reset(store, arguments or {}))]
            return [
                _text(
                    {"status": "error", "kind": "unknown_tool", "detail": f"unknown tool: {name}"}
                )
            ]
        except Exception as exc:  # noqa: BLE001 — never let an exception escape the turn
            return [
                _text(
                    {
                        "status": "error",
                        "kind": "sdk_error",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
            ]

    return server


async def _handle_ask(store: SessionStore, args: dict) -> dict:
    question = str(args.get("question") or "")
    context = str(args.get("context") or "")
    session = str(args.get("session") or "default")
    reset = bool(args.get("reset") or False)

    dumped: str | None = None
    if reset:
        dumped = store.reset(session, save=True)

    allowed, reason = guard.check(question, context)
    if not allowed:
        audit.record(
            decision="denied",
            stage="guard",
            reason=reason,
            question=question,
            context=context,
            session=session,
        )
        return {"status": "refused", "stage": "guard", "reason": reason}

    t0 = time.monotonic()
    res = await fable.run(question, context, resume=store.resume_id(session))
    duration_ms = int((time.monotonic() - t0) * 1000)

    if res.status == "refused":
        audit.record(
            decision="refused",
            stage="model",
            reason=res.text,
            question=question,
            context=context,
            session=session,
            duration_ms=duration_ms,
        )
        return {"status": "refused", "stage": "model", "reason": res.text}

    if res.status == "error":
        audit.record(
            decision="error",
            stage=None,
            reason=res.kind,
            question=question,
            context=context,
            session=session,
            duration_ms=duration_ms,
            outcome_detail=res.text,
        )
        return {"status": "error", "kind": res.kind, "detail": res.text}

    store.record_turn(session, question, res.text, res.session_id)
    audit.record(
        decision="allowed",
        stage=None,
        reason="ok",
        question=question,
        context=context,
        session=session,
        duration_ms=duration_ms,
    )
    payload: dict = {
        "status": "ok",
        "model": fable.FABLE_MODEL,
        "answer": res.text,
        "session": session,
    }
    if dumped:
        payload["reset_dump"] = dumped
    return payload


def _handle_reset(store: SessionStore, args: dict) -> dict:
    session = str(args.get("session") or "default")
    save = bool(args.get("save", True))
    dumped = store.reset(session, save=save)
    return {"status": "ok", "session": session, "cleared": True, "dump": dumped}
