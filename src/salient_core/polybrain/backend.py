"""PolybrainBackend — owns the multi-turn tool loop for API sub-brains.

Unlike CodexBackend (an external agent CLI fed tools through an MCP gateway),
this backend runs the loop itself: it streams nothing (v1), executes every
tool call through the kernel `ToolBundle` handlers, and emits ledger-paired
`ToolCallContent` / `ToolResultEvent` events the runner consumes for
observability.

Control honesty:
- **Scope** enforcement lives inside the factory-built bundle handlers
  (`salient_core.policy.scope.gate`) — free for any backend using the bundle.
- **Safeguards / approve_before** do NOT come free (they are Claude-SDK
  hooks; codex needed its own gate). `_make_runner` passes a `safeguard_hook`
  here and EVERY tool call is evaluated through it before the handler runs;
  a denial returns an error tool result and the handler is never invoked.
- **STOP**: pure HTTP backend, no `ReapableBackend` child — the runner
  reports `sdk_state="no_pid"`. `interrupt()` cancels the in-flight request
  and terminates `receive_response()` promptly so the quiesce ladder never
  wedges on us.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass

from ..runtime import (
    AgentEvent,
    AssistantContent,
    AssistantEvent,
    ContextUsage,
    JsonValue,
    ProviderErrorEvent,
    TextContent,
    ThinkingContent,
    ToolBundle,
    ToolCallContent,
    ToolResultEvent,
    TurnCompletedEvent,
    TurnUsage,
)
from .factory import BrainSpec, get_spec
from .models import find_model
from .openai_compat import BrainError
from .types import AssistantReply, Brain, BrainToolCall, ChatMessage

__all__ = ["PolybrainBackend", "PolybrainBackendConfig", "SafeguardHook"]

# Denial reason, or None to allow. Runs on the runner's event loop.
SafeguardHook = Callable[[str, Mapping[str, JsonValue]], Awaitable[str | None]]

# Mirrors polybrain's `stopWhen: stepCountIs(25)` tool-loop cap.
DEFAULT_MAX_TURNS = 25

_EMPTY_TOOL_BUNDLE = ToolBundle()


@dataclass(frozen=True, slots=True)
class PolybrainBackendConfig:
    brain: str
    model: str
    instructions: str | None = None
    agent_name: str = "polybrain"
    max_tokens: int = 8192
    temperature: float | None = None
    max_turns: int = DEFAULT_MAX_TURNS


class PolybrainBackend:
    def __init__(
        self,
        config: PolybrainBackendConfig,
        *,
        brain: Brain,
        spec: BrainSpec | None = None,
        tool_bundle: ToolBundle = _EMPTY_TOOL_BUNDLE,
        safeguard_hook: SafeguardHook | None = None,
    ) -> None:
        self._config = config
        self._brain = brain
        self._spec = spec or get_spec(config.brain)
        self._tools = {tool.name: tool for tool in tool_bundle.tools}
        self._tool_schemas: tuple[Mapping[str, JsonValue], ...] = tuple(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.input_schema),
                },
            }
            for tool in tool_bundle.tools
        )
        self._safeguard_hook = safeguard_hook
        self._history: list[ChatMessage] = []
        self._queue: asyncio.Queue[AgentEvent | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._interrupted = False
        self._closed = False
        self._cumulative = TurnUsage()
        self._last_reply_usage = 0

    # -- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        # The httpx client is created with the brain; there is no subprocess
        # to spawn and no handshake to perform.
        self._closed = False

    async def disconnect(self) -> None:
        self._closed = True
        await self.interrupt()
        await self._brain.aclose()

    # -- turn pump ---------------------------------------------------------

    async def query(self, prompt: str) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError(f"{self._config.agent_name}: a polybrain turn is already running")
        self._history.append(ChatMessage("user", content=prompt))
        self._queue = asyncio.Queue()
        self._interrupted = False
        self._task = asyncio.create_task(self._run_loop())

    def receive_response(self) -> AsyncIterator[AgentEvent]:
        return self._drain()

    async def _drain(self) -> AsyncIterator[AgentEvent]:
        queue = self._queue
        if queue is None:
            return
        while True:
            event = await queue.get()
            if event is None:
                return
            yield event

    async def interrupt(self) -> None:
        self._interrupted = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            # Drain so the CancelledError does not surface as an unretrieved
            # task exception; the loop's finally still closes the event stream.
            await asyncio.gather(task, return_exceptions=True)

    # -- the loop ----------------------------------------------------------

    def _wire_messages(self) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        if self._config.instructions:
            messages.append(ChatMessage("system", content=self._config.instructions))
        messages.extend(self._history)
        return messages

    async def _run_loop(self) -> None:
        queue = self._queue
        assert queue is not None
        started = time.monotonic()
        turns = 0
        try:
            while turns < self._config.max_turns and not self._interrupted:
                reply = await self._brain.chat(
                    messages=self._wire_messages(),
                    tools=self._tool_schemas,
                    max_tokens=self._config.max_tokens,
                    temperature=self._config.temperature,
                )
                turns += 1
                self._record_usage(reply)
                await self._emit(self._assistant_event(reply))
                self._history.append(
                    ChatMessage(
                        "assistant",
                        content=reply.text or None,
                        tool_calls=reply.tool_calls,
                        reasoning=reply.reasoning,
                    )
                )
                if not reply.tool_calls:
                    break
                for call in reply.tool_calls:
                    if self._interrupted:
                        break
                    content, is_error = await self._execute_tool(call)
                    await self._emit(
                        ToolResultEvent(tool_call_id=call.id, content=content, is_error=is_error)
                    )
                    self._history.append(ChatMessage("tool", content=content, tool_call_id=call.id))
            await self._emit(
                TurnCompletedEvent(
                    turns=turns,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    usage=self._cumulative,
                )
            )
        except BrainError as error:
            await self._emit(
                ProviderErrorEvent(code=error.code, message=str(error), retryable=error.retryable)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — last-resort honesty, never a hung run
            await self._emit(
                ProviderErrorEvent(
                    code="internal",
                    message=f"polybrain loop failed: {type(error).__name__}: {error}",
                )
            )
        finally:
            await queue.put(None)

    async def _emit(self, event: AgentEvent) -> None:
        queue = self._queue
        if queue is not None:
            await queue.put(event)

    def _assistant_event(self, reply: AssistantReply) -> AssistantEvent:
        content: list[AssistantContent] = []
        if reply.reasoning:
            content.append(ThinkingContent(reply.reasoning))
        if reply.text:
            content.append(TextContent(reply.text))
        for call in reply.tool_calls:
            content.append(ToolCallContent(id=call.id, name=call.name, arguments=call.arguments))
        return AssistantEvent(
            content=tuple(content), model=reply.model, stop_reason=reply.stop_reason
        )

    def _record_usage(self, reply: AssistantReply) -> None:
        usage = reply.usage
        self._last_reply_usage = usage.total_tokens or (usage.input_tokens + usage.output_tokens)
        window = self._context_window()
        self._cumulative = TurnUsage(
            input_tokens=self._cumulative.input_tokens + usage.input_tokens,
            output_tokens=self._cumulative.output_tokens + usage.output_tokens,
            reasoning_tokens=self._cumulative.reasoning_tokens + usage.reasoning_tokens,
            total_tokens=self._cumulative.input_tokens
            + usage.input_tokens
            + self._cumulative.output_tokens
            + usage.output_tokens,
            context_window=window,
        )

    def _context_window(self) -> int | None:
        info = find_model(self._spec.name, self._brain.model)
        return info.context_window if info is not None else None

    async def _execute_tool(self, call: BrainToolCall) -> tuple[str, bool]:
        tool = self._tools.get(call.name)
        if tool is None:
            return (f"unknown tool {call.name!r}", True)
        if self._safeguard_hook is not None:
            try:
                denial = await self._safeguard_hook(call.name, call.arguments)
            except Exception as error:  # noqa: BLE001 — a gate failure fails closed
                return (f"safeguard evaluation failed (fail-closed): {error}", True)
            if denial is not None:
                return (f"denied by safeguard policy: {denial}", True)
        try:
            result = await tool.handler(call.arguments)
        except Exception as error:  # noqa: BLE001 — tool errors feed back to the model
            return (f"{type(error).__name__}: {error}", True)
        if isinstance(result, str):
            return (result, False)
        return (json.dumps(result, default=str), False)

    # -- introspection ------------------------------------------------------

    async def get_context_usage(self) -> ContextUsage | None:
        window = self._context_window()
        if not window:
            return None
        used = self._last_reply_usage
        return ContextUsage(
            used_tokens=used,
            max_tokens=window,
            percentage=round(100.0 * used / window, 1),
            model=self._brain.model,
        )

    def diagnose_failure(
        self,
        agent_name: str,
        error: BaseException,
        stderr_tail: tuple[str, ...],
    ) -> str:
        if isinstance(error, BrainError):
            if error.code == "auth":
                envs = " / ".join(self._spec.api_key_envs)
                return f"{agent_name}: polybrain/{self._spec.name} auth failed — check {envs}"
            return f"{agent_name}: polybrain/{self._spec.name} {error}"
        return f"{agent_name}: polybrain backend failure: {type(error).__name__}: {error}"
