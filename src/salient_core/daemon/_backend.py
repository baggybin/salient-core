"""Default agent backend — a 1:1 passthrough over a local `ClaudeSDKClient`.

The `AgentBackend` Protocol lives in `salient_core/protocols.py` (the kernel's
interface surface); this module ships the concrete local implementation the
runner uses by default. A future `RemoteBackend` (a runner proxied over a socket
to another process/host) is a separate implementation of the same Protocol — the
local path stays direct/in-process, so only genuinely remote calls would ever
pay serialization.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..display import _stringify_tool_result
from ..providers import ProviderCapabilities, ProviderName, ProviderProbe
from ..runtime import (
    AgentEvent,
    AssistantContent,
    AssistantEvent,
    ContextUsage,
    JsonValue,
    TextContent,
    ThinkingContent,
    ToolBundle,
    ToolCallContent,
    ToolResultEvent,
    TurnCompletedEvent,
    TurnUsage,
)
from ._helpers import classify_run_loop_error


class ClaudeProviderConfigError(ValueError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"invalid Claude provider config: {detail}")


_EMPTY_TOOL_BUNDLE = ToolBundle()


def _json_value(value: Any) -> JsonValue:
    match value:
        case None | str() | int() | float() | bool():
            return value
        case Mapping():
            return {str(key): _json_value(item) for key, item in value.items()}
        case list() | tuple():
            return tuple(_json_value(item) for item in value)
        case _:
            return str(value)


def _usage_value(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key, 0)
    return int(value or 0)


class LocalClaudeBackend:
    """Default backend: a 1:1 passthrough over a local `ClaudeSDKClient`
    subprocess. No logic — every method forwards. This passthrough is what makes
    routing the runner through the `AgentBackend` seam a behavior-preserving
    refactor. Structurally satisfies `salient_core.protocols.AgentBackend`."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self._client = ClaudeSDKClient(options=options)

    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def query(self, prompt: str) -> None:
        await self._client.query(prompt)

    async def receive_response(self) -> AsyncIterator[AgentEvent]:
        async for message in self._client.receive_response():
            match message:
                case AssistantMessage():
                    content: list[AssistantContent] = []
                    for block in message.content:
                        match block:
                            case TextBlock():
                                content.append(TextContent(block.text))
                            case ThinkingBlock():
                                content.append(ThinkingContent(block.thinking))
                            case ToolUseBlock():
                                arguments = _json_value(block.input)
                                if isinstance(arguments, Mapping):
                                    content.append(ToolCallContent(block.id, block.name, arguments))
                            case _:
                                continue
                    yield AssistantEvent(
                        content=tuple(content),
                        model=message.model,
                        stop_reason=message.stop_reason,
                        error_code=message.error,
                    )
                case UserMessage():
                    blocks = message.content if isinstance(message.content, list) else []
                    for block in blocks:
                        match block:
                            case ToolResultBlock():
                                yield ToolResultEvent(
                                    tool_call_id=block.tool_use_id,
                                    content=_stringify_tool_result(block.content),
                                    is_error=bool(block.is_error),
                                )
                            case _:
                                continue
                case ResultMessage():
                    usage = message.usage if isinstance(message.usage, Mapping) else {}
                    yield TurnCompletedEvent(
                        turns=message.num_turns,
                        duration_ms=message.duration_ms,
                        usage=TurnUsage(
                            input_tokens=_usage_value(usage, "input_tokens"),
                            output_tokens=_usage_value(usage, "output_tokens"),
                            cache_read_tokens=_usage_value(usage, "cache_read_input_tokens"),
                            cache_create_tokens=_usage_value(usage, "cache_creation_input_tokens"),
                            cost_usd=message.total_cost_usd,
                        ),
                    )
                case _:
                    continue

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def get_context_usage(self) -> ContextUsage | None:
        usage = await self._client.get_context_usage()
        if usage is None:
            return None
        return ContextUsage(
            used_tokens=int(usage["totalTokens"]),
            max_tokens=int(usage["maxTokens"]),
            percentage=float(usage["percentage"]),
            model=str(usage["model"]),
        )

    def diagnose_failure(
        self,
        agent_name: str,
        error: BaseException,
        stderr_tail: tuple[str, ...],
    ) -> str:
        return classify_run_loop_error(
            agent_name,
            error,
            self._client,
            stderr_tail=stderr_tail,
        )


class ClaudeProvider:
    name = ProviderName("claude")
    capabilities = ProviderCapabilities(
        streaming=True,
        tools=True,
        interruption=True,
        context_usage=True,
    )

    def __init__(self, options: ClaudeAgentOptions | None = None) -> None:
        self._options = options

    async def probe(self) -> ProviderProbe:
        return ProviderProbe(available=True, detail="claude-agent-sdk installed")

    def create_backend(
        self,
        config: Mapping[str, JsonValue],
        *,
        tool_bundle: ToolBundle = _EMPTY_TOOL_BUNDLE,
    ) -> LocalClaudeBackend:
        # The Claude backend wires tools through pre-built ClaudeAgentOptions
        # (the default no-`runtime:` path), not the provider tool_bundle seam —
        # the bundle is only how neutral providers (Codex) receive tools. An
        # explicit `runtime: {provider: claude}` WITH a `tool:` block would build
        # a bundle and hand it here; silently dropping it produced a tool-less
        # backend while the runner still advertised the tools. Fail loud instead.
        if tool_bundle.tools:
            raise ClaudeProviderConfigError(
                "the Claude runtime does not accept a provider tool bundle; drop "
                "the explicit `runtime: {provider: claude}` block so tools wire "
                "through the default Claude path"
            )
        factory_owned = {"agent_name", "cwd", "instructions", "mcp_servers"}
        unknown = set(config) - {"model", *factory_owned}
        if unknown:
            raise ClaudeProviderConfigError(f"unknown fields: {', '.join(sorted(unknown))}")
        if self._options is not None:
            if config:
                raise ClaudeProviderConfigError(
                    "runtime config cannot override pre-built Claude options"
                )
            return LocalClaudeBackend(self._options)
        model = config.get("model")
        match model:
            case None:
                options = ClaudeAgentOptions()
            case str():
                options = ClaudeAgentOptions(model=model)
            case _:
                raise ClaudeProviderConfigError("model must be a string")
        return LocalClaudeBackend(options)
