"""Protocol seams for the kernel.

These Protocols define the injection points where a downstream application
(a domain skin, the tutor showcase, or any other consumer) plugs into
the kernel. The kernel ships no-op/stub defaults; the downstream provides
the real implementations.

Protocols:
    DaemonServices — the surface a runner may touch on its owning daemon.
    ToolBuilder    — callable that constructs a tool MCP server from a
                     factory type + config. A downstream skin provides the
                     real tool factories; the kernel ships a stub.
    AliasProtocol  — tool-name aliasing (e.g. wire→real name mapping). The
                     kernel ships IdentityAlias (no-op); a downstream skin
                     provides the real mapping.
    AgentBackend   — abstract agent SDK (v1: Claude SDK; v2 seam for
                     multi-SDK support).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DaemonServices(Protocol):
    """The surface of the owning Daemon that the bus tools and an
    AgentRunner may touch.

    This is the injection seam: a downstream that builds its own daemon
    and calls the public ``make_bus(daemon, owner)`` must satisfy this
    Protocol. It therefore declares the *whole* surface the bundled bus
    tools reach — not just the runner's read-only slice — so the
    annotation on ``make_bus`` is an honest contract rather than a
    partial hint. Members are typed ``Any`` where a fuller interface
    would drag daemon-internal types into this module; the inline
    comments name the real shapes.
    """

    # ── state the bus tools read ──────────────────────────────────────
    profile: dict[str, Any]
    engagement_path: Any
    context: Any  # ContextStore
    kg: Any  # KnowledgeGraph
    inbox: Any  # QuestionInbox
    actions: Any  # ActionLedger
    runners: dict[str, Any]  # name → AgentRunner
    all_cfgs: dict[str, dict[str, Any]]  # name → agents.yaml config
    event_hub: (
        Any  # EventHub — daemon-wide live event fan-out (kg contradiction notices publish here)
    )
    prompt_timeout: float | None
    _bus_calls: dict[int, Any] | None  # call_id → BusCall
    _endpoint_semaphores: dict[str, asyncio.Semaphore]  # per-host throttle

    # ── live event observation (multi-client attach point) ───────────
    # The stable seam for attaching observers — web overlays, tailers,
    # metrics exporters, or a downstream socket/WebSocket endpoint that
    # relays events to remote clients. Implementations delegate to the
    # daemon's ``EventHub`` (``daemon/_event_hub.py``), which returns a
    # bounded live queue plus a replay snapshot of recent backlog and
    # drops on overflow — a slow or remote subscriber must never stall
    # the agent that produced the event. Anything crossing a process or
    # network boundary attaches HERE, never inline in the dispatch path.
    def subscribe_events(self) -> tuple[asyncio.Queue, list[dict[str, Any]]]: ...
    def unsubscribe_events(self, q: asyncio.Queue) -> None: ...

    # ── operator-approval questions (each returns a future the tool awaits) ──
    def add_question(self, agent: str, text: str, job_id: int | None = None) -> int: ...
    def add_delegation_question(
        self, caller: str, target: str, prompt: str
    ) -> tuple[int, asyncio.Future]: ...
    def add_agent_start_question(
        self, caller: str, target: str, prompt: str
    ) -> tuple[int, asyncio.Future]: ...
    def add_redispatch_question(
        self, caller: str, target: str, prompt: str, *, consecutive: int
    ) -> tuple[int, asyncio.Future]: ...
    def add_swarm_fanout_question(
        self, caller: str, child_names: list[str]
    ) -> tuple[int, asyncio.Future]: ...
    def add_lesson_proposal_question(
        self, caller: str, text: str, kind: str
    ) -> tuple[int, asyncio.Future]: ...
    def add_skill_proposal(self, owner: str, spec: dict[str, Any]) -> int: ...

    # ── in-flight ask_agent call registry ─────────────────────────────
    def bus_call_admission_check(self, caller: str) -> str | None: ...
    def bus_call_register(
        self,
        caller: str,
        target: str,
        prompt: str,
        future: asyncio.Future,
        *,
        parent_call_id: int | None = None,
        swarm_role: str | None = None,
        initial_state: str = ...,
    ) -> int: ...
    def bus_call_set_state(self, call_id: int, state: str) -> None: ...
    def bus_call_set_future(self, call_id: int, future: asyncio.Future) -> None: ...
    def bus_call_set_child_job(self, call_id: int, job_id: int) -> None: ...
    def bus_call_resolve(self, call_id: int) -> None: ...
    def bus_call_cancel(
        self,
        caller: str | None = None,
        target: str | None = None,
        *,
        parent_call_id: int | None = None,
        call_id: int | None = None,
    ) -> list[int]: ...

    # ── redispatch accounting ─────────────────────────────────────────
    def _redispatch_threshold(self) -> int: ...
    def _redispatch_swarm_min(self) -> int: ...
    def _redispatch_check(self, caller: str, target: str) -> int: ...
    def _redispatch_increment(self, caller: str, target: str) -> None: ...
    def _redispatch_spend_one(self, caller: str, target: str) -> None: ...
    def _redispatch_grant_credit(self, caller: str, target: str, n: int) -> None: ...

    # ── agent lifecycle ───────────────────────────────────────────────
    def expand_prompt(self, text: str) -> tuple[str, list[tuple[str, str]]]: ...
    def _make_runner(self, cfg: dict[str, Any]) -> Any: ...
    def _notify_agent_spawn(self, name: str, cfg: dict[str, Any], runner: Any) -> None: ...
    async def _notify_agent_despawn(self, name: str) -> None: ...
    def _persist_running_agents(self) -> None: ...
    async def start_agent(self, name: str) -> None: ...
    async def _swarm_teardown(self, owner: str, *, reason: str) -> None: ...


@runtime_checkable
class ToolBuilder(Protocol):
    """Callable that builds a tool MCP server from a factory type + config.

    A downstream skin provides the real implementation (its tool
    factories). The kernel ships a stub that raises NotImplementedError.
    """

    def __call__(
        self,
        tool_type: str,
        config: dict[str, Any],
        *,
        server_name: str | None = None,
    ) -> tuple[Any, str, list[str]]:
        """Return (mcp_server, wire_name, builtin_tool_names)."""
        ...


@runtime_checkable
class AliasProtocol(Protocol):
    """Tool-name aliasing (wire name ↔ real name mapping).

    The kernel ships IdentityAlias (no-op passthrough). A downstream skin
    provides the real mapping (e.g. ``fetch`` → ``http-get``).
    """

    def to_wire(self, name: str) -> str: ...
    def to_real(self, name: str) -> str: ...
    def rewrite_outbound(self, text: str) -> str: ...
    def rewrite_inbound(self, text: str) -> str: ...
    def mapping(self) -> dict[str, str]: ...
    def enabled(self) -> bool: ...


@runtime_checkable
class AgentBackend(Protocol):
    """The exact surface `AgentRunner` needs from an agent SDK client — kept
    minimal and faithful to real usage (seven members, no more). Anything that
    crosses a process/network boundary implements this; the local default
    (``daemon/_backend.py`` ``LocalClaudeBackend``) is a direct passthrough over
    a ``ClaudeSDKClient`` subprocess.

    v2 multi-SDK note: a future backend might wrap OpenAI's Responses API,
    Gemini, or a local model server — it only needs these seven members.
    """

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[Any]:
        """Return the async iterator of response messages for the in-flight
        turn. NOT a coroutine — callers do
        ``async for msg in backend.receive_response()``."""
        ...

    async def interrupt(self) -> None: ...
    async def get_context_usage(self) -> dict[str, Any] | None: ...

    @property
    def raw(self) -> Any:
        """The underlying client object, for the death-probe in
        ``classify_run_loop_error`` (which reads the subprocess returncode). A
        remote backend returns whatever object carries its own liveness signal."""
        ...
