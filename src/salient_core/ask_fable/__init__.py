"""ask_fable — a gated MCP tool for requesting Fable (claude-fable-5) reasoning.

Exposes ``mcp__ask_fable__ask`` (and ``reset_session``) so any agent, in Claude
Code or opencode, can request narrow code/architecture reasoning from Fable. A
two-layer guard (a deterministic broadness/shape heuristic + the kernel's reused
prohibited-use denylist) rejects broad or off-scope questions before the model is
ever called; Fable's system prompt enforces scope as a final layer.

Run it: ``python3 -m salient_core.ask_fable`` (or the ``ask-fable`` console
script). Requires the optional ``mcp`` dependency (``pip install
'salient-core[ask-fable]'``).
"""

from __future__ import annotations

from .server import build_server

__all__ = ["build_server", "serve"]


def serve() -> None:
    """Start the server over stdio (the default per-developer transport)."""
    import asyncio

    try:
        from mcp.server.stdio import stdio_server
    except ImportError as e:  # pragma: no cover - import-time guard
        raise ImportError("mcp not installed. Run: pip install 'salient-core[ask-fable]'") from e

    server = build_server()

    async def _main() -> None:
        async with stdio_server() as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    asyncio.run(_main())
