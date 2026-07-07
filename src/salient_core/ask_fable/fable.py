"""Invoke the Fable model (claude-fable-5) for a single reasoning turn.

Primary path is the Claude Agent SDK in-process, mirroring salient-core's
``daemon/_backend.py`` ``LocalClaudeBackend``: it reuses Claude Code's existing
OAuth session (``~/.claude/.credentials.json``) — we deliberately do NOT set
``ANTHROPIC_API_KEY``. Tools are disabled so this is a pure reasoning oracle.

Multi-turn: pass ``resume=<session_id>`` (captured from a prior turn's
``ResultMessage.session_id``) to continue a conversation — Fable keeps context
server-side, so we never re-send the transcript.

Fallback path shells out to the ``claude`` CLI in print mode (same OAuth), for
environments without the SDK. The CLI fallback is single-turn only (text output
carries no resumable session id).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass

from .prompts import FABLE_SYSTEM_PROMPT

FABLE_MODEL = "claude-fable-5"


@dataclass
class FableResult:
    """Outcome of one Fable turn. ``status`` is one of ok|refused|error.

    - ok:      ``text`` is the answer; ``session_id`` set when resumable.
    - refused: ``text`` is the one-line reason (after the model's ``REFUSED:``).
    - error:   ``kind`` in {timeout, sdk_error, binary_missing}; ``text`` = detail.
    """

    status: str
    text: str = ""
    kind: str = ""
    session_id: str | None = None
    returncode: int | None = None


def _timeout_default() -> float:
    try:
        return float(os.environ.get("ASK_FABLE_TIMEOUT") or 120.0)
    except (TypeError, ValueError):
        return 120.0


def _compose(question: str, context: str) -> str:
    q = (question or "").strip()
    ctx = (context or "").strip()
    if ctx:
        return f"QUESTION:\n{q}\n\nCODE CONTEXT:\n{ctx}"
    return f"QUESTION:\n{q}"


def _shape(text: str, session_id: str | None = None) -> FableResult:
    """Map raw model text to a FableResult, honoring the REFUSED contract."""
    text = (text or "").strip()
    if not text:
        return FableResult("error", kind="sdk_error", text="empty response from Fable")
    if text.startswith("REFUSED:"):
        reason = text[len("REFUSED:") :].strip() or "off-scope"
        return FableResult("refused", text=reason, session_id=session_id)
    return FableResult("ok", text=text, session_id=session_id)


async def run(
    question: str,
    context: str = "",
    *,
    resume: str | None = None,
    timeout: float | None = None,
    use_cli: bool | None = None,
) -> FableResult:
    """Run one Fable turn. Returns a FableResult (never raises for expected
    failures). ``resume`` continues a prior SDK session (ignored by the CLI path)."""
    timeout = timeout if timeout is not None else _timeout_default()
    if use_cli is None:
        use_cli = (os.environ.get("ASK_FABLE_USE_CLI") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    message = _compose(question, context)
    if use_cli:
        return await _run_cli(message, timeout)
    try:
        return await _run_sdk(message, timeout, resume)
    except ImportError:
        # SDK unavailable — degrade to the CLI bridge (single-turn).
        return await _run_cli(message, timeout)


async def _run_sdk(message: str, timeout: float, resume: str | None) -> FableResult:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    options = ClaudeAgentOptions(
        model=FABLE_MODEL,
        system_prompt=FABLE_SYSTEM_PROMPT,
        allowed_tools=[],  # pure reasoning — no tools
        mcp_servers={},
        strict_mcp_config=True,  # ignore ambient MCP config
        max_turns=1,
        setting_sources=[],  # don't load user/project CLAUDE.md/settings
        resume=resume,  # continue a prior conversation when set
    )

    async def _drive() -> tuple[str, str | None, str | None]:
        parts: list[str] = []
        err: str | None = None
        session_id: str | None = None
        client = ClaudeSDKClient(options=options)
        await client.connect()
        try:
            await client.query(message)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "error", None):
                        err = str(msg.error)
                    for block in msg.content or []:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    session_id = getattr(msg, "session_id", None)
                    if getattr(msg, "is_error", False):
                        err = err or (
                            getattr(msg, "api_error_status", None)
                            or getattr(msg, "subtype", None)
                            or "result error"
                        )
                    break
        finally:
            await client.disconnect()
        return "".join(parts).strip(), err, session_id

    try:
        text, err, session_id = await asyncio.wait_for(_drive(), timeout)
    except TimeoutError:
        return FableResult("error", kind="timeout", text=f"Fable timed out after {timeout:.0f}s")
    if err:
        return FableResult("error", kind="sdk_error", text=str(err), session_id=session_id)
    return _shape(text, session_id)


async def _run_cli(message: str, timeout: float) -> FableResult:
    claude = shutil.which("claude")
    if not claude:
        return FableResult(
            "error",
            kind="binary_missing",
            text="`claude` CLI not found on PATH (and the Claude Agent SDK was unavailable)",
        )
    argv = [
        claude,
        "-p",
        "--model",
        FABLE_MODEL,
        "--system-prompt",
        FABLE_SYSTEM_PROMPT,
        "--tools",
        "",  # disable every tool
        "--strict-mcp-config",  # + no --mcp-config => zero MCP tools
        "--output-format",
        "text",
    ]

    def _call() -> subprocess.CompletedProcess[str]:
        # Question on stdin (no shell, no argv injection, no ARG_MAX limit).
        return subprocess.run(
            argv,
            input=message,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    try:
        proc = await asyncio.to_thread(_call)
    except subprocess.TimeoutExpired:
        return FableResult(
            "error", kind="timeout", text=f"Fable CLI timed out after {timeout:.0f}s"
        )
    except FileNotFoundError:
        return FableResult("error", kind="binary_missing", text="`claude` CLI not found on PATH")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:500] or "claude CLI returned non-zero"
        return FableResult("error", kind="sdk_error", text=detail, returncode=proc.returncode)
    return _shape(proc.stdout)
