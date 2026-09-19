"""A provider runtime must not be told it has Claude Code built-ins.

The tools block (`_format_tools_block`) rendered `builtin_tools` verbatim for
both runtimes. The SDK path registers them in `ClaudeAgentOptions`; a provider
runtime (polybrain/codex) serves only its ToolBundle and has none of them, so
the prompt sent the model after tools that answer `unknown tool` — measured
2026-09-19 in a provider seat's transcript. The block is now runtime-aware.
"""

from __future__ import annotations

from typing import Any

from salient_core.daemon._prompts import _format_tools_block

_PROVIDER = {"name": "polybrain", "brain": "deepseek"}


def _cfg(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "mapper",
        "tool": {"type": "fs"},
        "builtin_tools": ["Read", "Grep", "Glob"],
    }
    base.update(over)
    return base


def test_sdk_path_advertises_the_declared_builtins() -> None:
    block = _format_tools_block(_cfg())
    assert "Built-in Claude Code tools available: Read, Grep, Glob" in block
    # ...and does not simultaneously call them unavailable.
    assert "NOT available" not in block or "Read" not in block.split("NOT available", 1)[1]


def test_provider_path_lists_them_as_unavailable_instead() -> None:
    block = _format_tools_block(_cfg(runtime=_PROVIDER))
    assert "Built-in Claude Code tools available" not in block
    unavailable = block.split("NOT available", 1)[1]
    for name in ("Read", "Grep"):
        assert name in unavailable, f"{name} must be declared unavailable on the provider path"


def test_the_primary_tool_line_survives_on_both_runtimes() -> None:
    for over in ({}, {"runtime": _PROVIDER}):
        block = _format_tools_block(_cfg(**over))
        assert "PRIMARY action tool" in block
