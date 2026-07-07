"""Inter-agent communication bus.

Each agent gets its own bus MCP server bound to its identity, exposing:
  - context_write(key, value)   — write to the caller's namespace
  - context_read(agent, key)    — read from any agent's namespace
  - context_list(filter)        — list keys (filter='' or '*' = all agents)
  - ask_agent(name, prompt)     — queue a prompt to another agent and wait
  - list_agents(filter)         — names + status of running agents

`ContextStore` is plain in-memory and shared across all agents in the daemon.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

if TYPE_CHECKING:
    from ..protocols import DaemonServices


from ._audit import make_audit_tools
from ._common import *  # noqa: F401,F403
from ._consensus import make_consensus_tools
from ._context import make_context_tools
from ._context_store import _SCHEMA, ContextStore  # noqa: F401
from ._delegation import (  # noqa: F401
    make_delegation_tools,
    set_agent_disabled_checker,
    set_delegation_observer,
)
from ._discovery import make_discovery_tools
from ._flags import BusFlags  # noqa: F401
from ._kg import make_kg_tools, set_kg_assert_hook  # noqa: F401
from ._lessons import make_lessons_tools
from ._lifecycle import make_lifecycle_tools
from ._skills import make_skills_tools

# Curated public surface for `from salient_core.bus import *`. Explicit imports
# of the private `_*` helpers (used by the daemon package + tests) still work —
# `__all__` only bounds star-imports, not attribute access.
__all__ = [
    "ContextStore",
    "get_bus_builder",
    "make_bus",
    "make_bus_tools",
    "set_agent_disabled_checker",
    "set_bus_builder",
    "set_delegation_observer",
    "set_kg_assert_hook",
]

_BUS_TOOL_NAMES = (
    "context_write",
    "context_read",
    "context_list",
    "context_grep",
    "context_section",
    "context_head",
    "context_tail",
    "context_lines",
    "context_count",
    "context_summary",
    "ask_agent",
    "ask_agents",
    "ask_partner",
    "ask_consensus",
    "list_agents",
    "ask_operator",
    "search_skills",
    "get_skill",
    "kg_assert",
    "kg_query",
    "kg_neighbors",
    "kg_stats",
    "kg_semantic_query",
    "record_review",
    "propose_lesson",
    "propose_skill",
    "rule_validate",
    "read_evidence",
    "prior_actions",
    "spawn_template",
    "swarm_finish",
)


def make_bus(
    daemon: DaemonServices,
    owner: str,
    *,
    extra_tools: list[BusTool] | None = None,
) -> tuple[McpSdkServerConfig, str, list[str]]:
    """Build a bus MCP server for one agent. Returns (server, server_name, wire_names).

    ``extra_tools`` is a generic extension slot: a caller (via the
    ``set_bus_builder`` seam) may append its own ``@bus_tool`` closures — e.g. a
    downstream skin's domain tool — after the built-ins, before the SDK server is
    created. The kernel stays agnostic to what they are; their wire names derive
    from each tool's ``.name`` (one source of truth) and a collision with a
    built-in raises rather than silently shadowing it in the opaque MCP server."""

    (
        context_write,
        context_read,
        context_list,
        context_grep,
        context_section,
        context_head,
        context_tail,
        context_lines,
        context_count,
        context_summary,
    ) = make_context_tools(daemon, owner)

    (
        ask_agent,
        ask_agents,
        ask_partner,
        ask_operator,
    ) = make_delegation_tools(daemon, owner)

    # Consensus dispatches each leg through ask_agent, so it's built after the
    # delegation tools and handed the ask_agent closure.
    (ask_consensus,) = make_consensus_tools(daemon, owner, ask_agent)

    (
        list_agents,
        search_skills,
        get_skill,
    ) = make_discovery_tools(daemon, owner)

    (
        kg_assert,
        kg_query,
        kg_neighbors,
        kg_stats,
        kg_semantic_query,
        record_review,
    ) = make_kg_tools(daemon, owner)

    (propose_lesson,) = make_lessons_tools(daemon, owner)

    (propose_skill,) = make_skills_tools(daemon, owner)

    (
        read_evidence,
        prior_actions,
        rule_validate,
    ) = make_audit_tools(daemon, owner)

    (
        spawn_template,
        swarm_finish,
    ) = make_lifecycle_tools(daemon, owner)

    # Alias the owner so the SDK-side server name is bus__<alias>__*
    # instead of bus__*****__*. The internal closures (context_write
    # etc.) keep using the real owner — context storage and KG-fact
    # attribution stay keyed on the operator-visible agent name. The
    # PreToolUse hooks reverse-alias when constructing the qualified
    # lookup key so safeguards still fire correctly.
    from ..alias import to_wire as _alias_to_wire

    server_name = f"bus__{_alias_to_wire(owner)}"
    # Assemble the bus tool closure list in the same order as
    # _BUS_TOOL_NAMES so make_bus_tools (below) can return them
    # paired with bare wire names.
    bus_tool_fns = [
        context_write,
        context_read,
        context_list,
        context_grep,
        context_section,
        context_head,
        context_tail,
        context_lines,
        context_count,
        context_summary,
        ask_agent,
        ask_agents,
        ask_partner,
        ask_consensus,
        list_agents,
        ask_operator,
        search_skills,
        get_skill,
        kg_assert,
        kg_query,
        kg_neighbors,
        kg_stats,
        kg_semantic_query,
        record_review,
        propose_lesson,
        propose_skill,
        rule_validate,
        read_evidence,
        prior_actions,
        spawn_template,
        swarm_finish,
    ]
    bare_names = list(_BUS_TOOL_NAMES)
    if extra_tools:
        built_in = set(bare_names)
        for t in extra_tools:
            if t.name in built_in:
                raise ValueError(
                    f"extra bus tool {t.name!r} collides with a built-in bus tool "
                    "— a duplicate wire name would silently shadow it in the MCP server"
                )
            bus_tool_fns.append(t)
            bare_names.append(t.name)
    server = create_sdk_mcp_server(
        name=server_name,
        version="0.1.0",
        tools=bus_tool_fns,
    )
    wire_names = [f"mcp__{server_name}__{t}" for t in bare_names]
    # If a make_bus_tools() call is currently in flight on this stack,
    # hand the freshly-built closure list to it via the contextvar
    # sink instead of stashing on the returned dict. The SDK
    # JSON-serializes the dict (mcpServers) when launching the
    # subprocess CLI — extra keys with non-serializable values break
    # that path. See the contextvar definition below for the why.
    sink = _BUS_TOOLS_SINK.get()
    if sink is not None and not sink:
        sink.append((bus_tool_fns, bare_names))
    return server, server_name, wire_names


# Side channel for make_bus_tools → make_bus. Daemon agents do not
# touch this; only make_bus_tools sets it on its frame to pull the
# closure list out of make_bus without stashing on the returned
# config dict (which the SDK serializes to JSON for the subprocess
# CLI, and SdkMcpTool instances aren't JSON-serializable).
_BUS_TOOLS_SINK: ContextVar[list[tuple[list, list[str]]] | None] = ContextVar(
    "_BUS_TOOLS_SINK",
    default=None,
)


def make_bus_tools(daemon: DaemonServices, owner: str) -> tuple[list, list[str]]:
    """Build just the per-agent bus tool closures (without wrapping them
    in an MCP server). Returns (tool_fns, bare_wire_names) zipped
    1-to-1 in _BUS_TOOL_NAMES order.

    Used to mirror the bus tools onto the agent's tool-type MCP server
    so the model can call read_evidence / ask_agent / context_* via
    EITHER namespace — `mcp__<alias>__<tool>` (the tool-type server)
    or `mcp__bus__<alias>__<tool>` (the dedicated bus server). Claude
    occasionally drops the `bus__` segment when guessing which server
    hosts a bus tool (see truncate.py:65-70 for the read_evidence
    case); registering on both servers turns the namespace confusion
    into a no-op instead of a `tool_use_error`.

    Each call returns FRESH closure instances bound to (daemon, owner)
    so the tool-type server and the bus server can hold distinct
    registrations of the same tools without interfering with each
    other's lifetime.
    """
    sink: list[tuple[list, list[str]]] = []
    token = _BUS_TOOLS_SINK.set(sink)
    try:
        # Build a fresh bus server purely to construct + capture
        # the closures; the server itself is discarded. Route through the
        # registered builder (not bare make_bus) so a skin's extra_tools are
        # mirrored here too, keeping the two tool namespaces in lockstep.
        get_bus_builder()(daemon, owner)
    finally:
        _BUS_TOOLS_SINK.reset(token)
    if not sink:
        return [], list(_BUS_TOOL_NAMES)
    tool_fns, bare = sink[0]
    return list(tool_fns), list(bare)


# ── Bus-builder seam ─────────────────────────────────────────────────
# The runner-factory builds each agent's bus via the REGISTERED builder, not
# `make_bus` directly, so a downstream skin can wrap it (e.g. to append its own
# tools via `extra_tools`) without the kernel importing anything skin-shaped.
# Default is the kernel's own `make_bus`; a skin registers its wrapper at import
# time, same idiom as alias.set_active / set_tool_builder / set_bus_builder is
# read at call time so registration order relative to import is irrelevant.
_bus_builder: Callable[..., tuple[McpSdkServerConfig, str, list[str]]] | None = None


def set_bus_builder(
    builder: Callable[..., tuple[McpSdkServerConfig, str, list[str]]],
) -> None:
    """Register the bus builder the runner-factory calls. The builder takes
    ``(daemon, owner)`` and returns ``make_bus``'s ``(server, name, wires)``
    tuple. Default is the kernel's ``make_bus``; a skin registers a wrapper."""
    global _bus_builder
    _bus_builder = builder


def get_bus_builder() -> Callable[..., tuple[McpSdkServerConfig, str, list[str]]]:
    """The active bus builder (the kernel's ``make_bus`` until a skin registers)."""
    return _bus_builder or make_bus
