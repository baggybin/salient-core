from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as _dataclass_replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, runtime_checkable

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
ToolHandler: TypeAlias = Callable[[Mapping[str, JsonValue]], Awaitable[JsonValue]]


class MissingBackendError(RuntimeError):
    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        super().__init__(f"{agent_name}: no agent backend configured")


# Annotations stamped on every provider-runtime tool by the daemon's
# `_gate_provider_bundle`. They live HERE, beside `AgentTool`, so a backend or
# gateway can read them without importing the daemon package (which would be
# circular). `POLICY_GATE` is the assertable marker that a tool is gated;
# `POLICY_GATE_BUDGET` tells a provider that imposes its own per-call deadline
# how many of those seconds may be spent waiting on a HUMAN rather than on the
# tool — without it, a provider ceiling silently truncates the operator's
# approval window.
POLICY_GATE_ANNOTATION: str = "salient/policy_gated"
POLICY_GATE_BUDGET_ANNOTATION: str = "salient/gate_budget_sec"


class PolicyDenied(PermissionError):
    """A provider-runtime tool call refused by the policy gate.

    RAISED (not returned) by the gate wrapper that
    ``AgentRunnerFactory._gate_provider_bundle`` installs on every
    provider-runtime ``AgentTool.handler``. Two deliberate properties:

    - **Raising fails closed.** A returned denial can be ignored by a backend
      that discards handler results; an exception cannot be silently dropped.
      Any provider that calls the handler at all inherits the refusal.
    - **``PermissionError`` is an ``OSError`` subclass**, which the codex MCP
      gateway's dispatch already catches and renders as an ``isError: True``
      tool result. A denial therefore reaches the model as a visible ERROR
      rather than as a successful result whose text happens to say "denied" —
      no new catch clause required, and no green-painted refusal.
    """

    def __init__(self, reason: str, *, tool: str = "", agent: str = "") -> None:
        self.reason = reason
        self.tool = tool
        self.agent = agent
        super().__init__(reason)


def _freeze_json(value: JsonValue) -> JsonValue:
    match value:
        case Mapping():
            return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
        case str() | int() | float() | bool() | None:
            return value
        case _:
            return tuple(_freeze_json(item) for item in value)


@dataclass(frozen=True, slots=True)
class AgentTool:
    name: str
    description: str
    input_schema: Mapping[str, JsonValue]
    handler: ToolHandler
    annotations: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            MappingProxyType(
                {key: _freeze_json(value) for key, value in self.input_schema.items()}
            ),
        )
        object.__setattr__(
            self,
            "annotations",
            MappingProxyType({key: _freeze_json(value) for key, value in self.annotations.items()}),
        )


@dataclass(frozen=True, slots=True)
class ToolBundle:
    tools: tuple[AgentTool, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self.tools))
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool bundle contains duplicate names")

    def select(self, allowed: frozenset[str]) -> ToolBundle:
        return ToolBundle(tuple(tool for tool in self.tools if tool.name in allowed))


# One PreToolUse-shaped policy check: `(input_data, tool_use_id, context)` in,
# an SDK hook envelope (or None/{}) out. Deliberately the SAME signature the
# Claude-SDK hooks use, so a host can pass the real hook callables straight in
# rather than adapting — adapting is how a mirror starts drifting.
PolicyCheck: TypeAlias = Callable[
    [Mapping[str, Any], str, Any], Awaitable[Mapping[str, Any] | None]
]


def gate_tool_bundle(
    bundle: ToolBundle,
    *,
    agent_name: str,
    server: str,
    checks: Sequence[PolicyCheck],
    gate_budget_sec: int = 0,
) -> ToolBundle:
    """Return `bundle` with every handler wrapped so `checks` run BEFORE it.

    A backend that executes `ToolBundle` handlers directly — the codex MCP
    gateway, the polybrain tool loop, any future provider — gets no
    Claude-SDK PreToolUse hooks, because those are registered in
    `ClaudeAgentOptions` on the SDK path only. Scope survives (it lives inside
    the built handler); safeguards, `approve_before` and the token-budget gate
    do not. This is the seam that puts them back.

    It lives HERE, beside `AgentTool`, and takes its checks as a parameter, so
    any host implementing the daemon-services surface can apply it — not only
    the one that happens to compose `AgentRunnerFactory`. `salient-tutor` runs
    its own `TutorDaemon` with its own `_make_runner`, and would otherwise
    reimplement this seam (and its bugs) a third time.

    Args:
        bundle: the tools to wrap. Returned unchanged when empty.
        agent_name: the calling agent, for denial attribution.
        server: the MCP server name the SDK path would use for this agent —
            normally the (aliased) agent name. See the qualified-name note below.
        checks: run in order; the FIRST deny wins and the handler never runs.
            Order matters: put cheap non-interactive checks first so a call
            that will be refused anyway never blocks on a human.
        gate_budget_sec: seconds of the eventual per-call deadline that belong
            to a human rather than the tool. Published as an annotation for a
            provider that imposes its own timeout; 0 to omit.

    Raises:
        PolicyDenied: from the wrapped handler, when a check denies. Raised
            rather than returned so a backend that discards handler results
            still fails closed.
    """
    if not bundle.tools:
        return bundle
    gated: list[AgentTool] = []
    for tool in bundle.tools:
        annotations: dict[str, Any] = dict(tool.annotations)
        annotations[POLICY_GATE_ANNOTATION] = True
        if gate_budget_sec:
            annotations[POLICY_GATE_BUDGET_ANNOTATION] = gate_budget_sec
        gated.append(
            _dataclass_replace(
                tool,
                handler=_gated_handler(tool, agent_name, server, checks),
                annotations=annotations,
            )
        )
    return ToolBundle(tuple(gated))


def _gated_handler(
    tool: AgentTool,
    agent_name: str,
    server: str,
    checks: Sequence[PolicyCheck],
) -> ToolHandler:
    """Wrap one handler so policy runs before it — or instead of it."""
    original = tool.handler
    # The SDK path sees `mcp__<server>__<tool>`, and the safeguard hook BRANCHES
    # on that prefix: MCP-form → safeguards then allow (scope is already inside
    # the handler); bare form → the built-in policy path, which runs a SECOND
    # scope evaluation and consults `trusted_builtins`. Provider bundles carry
    # bare wire names, so we synthesize the MCP form to get LITERAL parity with
    # the SDK path — same qualified_name, same dataset lookups. Passing bare
    # names would quietly take the builtin branch and double-evaluate scope: a
    # gate that looks armed and classifies wrong.
    qualified = f"mcp__{server}__{tool.name}"

    async def handler(arguments: Mapping[str, JsonValue]) -> JsonValue:
        tool_input: dict[str, Any] = dict(arguments or {})
        # Unique per invocation. A replay cache keyed on tool_use_id REJECTS a
        # reused id whose arguments differ; there is no SDK-issued id on this
        # path, so a fresh one per call is both correct and the only way two
        # concurrent calls to the same tool don't collide into a spurious
        # replay rejection.
        tool_use_id = f"provider-{uuid.uuid4().hex}"
        for check in checks:
            outcome = await check(
                {"tool_name": qualified, "tool_input": tool_input},
                tool_use_id,
                None,
            )
            specific = (outcome or {}).get("hookSpecificOutput") or {}
            if specific.get("permissionDecision") == "deny":
                raise PolicyDenied(
                    str(specific.get("permissionDecisionReason") or "denied by policy"),
                    tool=tool.name,
                    agent=agent_name,
                )
            # `approve_before` returns the operator's EDITED command (and an
            # injected `confirm_destructive`) under `updatedInput`. Dropping it
            # would run the ORIGINAL command while the operator was told their
            # edit applied — so thread it into both the remaining checks and
            # the eventual call.
            updated = specific.get("updatedInput")
            if isinstance(updated, Mapping):
                tool_input = dict(updated)
        return await original(tool_input)

    return handler


@dataclass(frozen=True, slots=True)
class TextContent:
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingContent:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallContent:
    id: str
    name: str
    arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "arguments",
            MappingProxyType({key: _freeze_json(value) for key, value in self.arguments.items()}),
        )


AssistantContent: TypeAlias = TextContent | ThinkingContent | ToolCallContent


@dataclass(frozen=True, slots=True)
class AssistantEvent:
    content: tuple[AssistantContent, ...]
    model: str
    stop_reason: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", tuple(self.content))


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    tool_call_id: str
    content: str
    is_error: bool = False


class NativeActionKind(StrEnum):
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    PERMISSION = "permission"
    # An MCP tool call the provider ran on the agent's behalf (e.g. a codex agent
    # calling a salient bus tool through the MCP gateway). Surfaced so the runner
    # publishes it as a tool-call / tool-result event like any other tool use.
    MCP_TOOL = "mcp_tool"


@dataclass(frozen=True, slots=True)
class NativeActionStartedEvent:
    id: str
    kind: NativeActionKind
    name: str
    arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "arguments",
            MappingProxyType({key: _freeze_json(value) for key, value in self.arguments.items()}),
        )


@dataclass(frozen=True, slots=True)
class NativeActionCompletedEvent:
    id: str
    kind: NativeActionKind
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int | None = None
    context_window: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class ContextUsage:
    used_tokens: int
    max_tokens: int
    percentage: float
    model: str | None = None


@dataclass(frozen=True, slots=True)
class TurnCompletedEvent:
    turns: int
    duration_ms: int
    usage: TurnUsage = field(default_factory=TurnUsage)


@dataclass(frozen=True, slots=True)
class ProviderErrorEvent:
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ContextCompactedEvent:
    summary: str


AgentEvent: TypeAlias = (
    AssistantEvent
    | ToolResultEvent
    | NativeActionStartedEvent
    | NativeActionCompletedEvent
    | TurnCompletedEvent
    | ProviderErrorEvent
    | ContextCompactedEvent
)


@runtime_checkable
class AgentBackend(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> AsyncIterator[AgentEvent]: ...
    async def interrupt(self) -> None: ...
    async def get_context_usage(self) -> ContextUsage | None: ...
    def diagnose_failure(
        self,
        agent_name: str,
        error: BaseException,
        stderr_tail: tuple[str, ...],
    ) -> str: ...
