from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeAlias, runtime_checkable

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
ToolHandler: TypeAlias = Callable[[Mapping[str, JsonValue]], Awaitable[JsonValue]]


class MissingBackendError(RuntimeError):
    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        super().__init__(f"{agent_name}: no agent backend configured")


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
