"""Shared types for the polybrain multi-vendor API runtime.

Python port of `../polybrain` (TypeScript) `src/providers/types.ts` — design
reference only; this is a reimplementation, not a transpile. A `Brain` is a
thin chat-level client (one vendor, one base URL); the *backend* owns the
agent turn loop and tool execution (`backend.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..runtime import JsonValue

__all__ = [
    "AssistantReply",
    "Brain",
    "BrainToolCall",
    "ChatMessage",
    "ModelInfo",
    "Usage",
]


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    label: str
    description: str = ""
    supports_vision: bool = False
    is_reasoning: bool = False
    context_window: int | None = None


@dataclass(frozen=True, slots=True)
class BrainToolCall:
    id: str
    name: str
    arguments: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn of conversation state kept by the backend.

    `tool_calls` is set on assistant messages that requested tool execution;
    `tool_call_id` is set on tool-result messages answering one call.
    `reasoning` carries a reasoning model's thinking trace for history.
    """

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    tool_calls: tuple[BrainToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AssistantReply:
    text: str
    reasoning: str | None
    tool_calls: tuple[BrainToolCall, ...]
    usage: Usage
    model: str
    stop_reason: str | None = None


class Brain(Protocol):
    """Thin chat-level protocol — NOT the agent loop (the backend owns that)."""

    name: str
    model: str

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, JsonValue]] = (),
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AssistantReply: ...

    def list_models(self) -> list[ModelInfo]: ...

    async def aclose(self) -> None: ...
