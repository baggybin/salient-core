"""OpenAI-compatible chat.completions client (httpx, non-streaming v1).

Port of `../polybrain` `src/providers/openai-compatible.ts`. Deliberately
non-streaming in v1: the kernel event contract does not require deltas (codex
coalesces into one `AssistantEvent` per message too), so the provider reports
`streaming=False` honestly. SSE streaming can be added behind the same
`chat()` signature later.

Reasoning models are first-class: `reasoning_content` (deepseek-reasoner,
glm) and `reasoning_details` (MiniMax) are extracted into
`AssistantReply.reasoning` so the backend can emit `ThinkingContent` instead
of silently dropping the trace.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from ..runtime import JsonValue
from .factory import BrainSpec
from .types import AssistantReply, BrainToolCall, ChatMessage, ModelInfo, Usage

__all__ = ["BrainError", "OpenAICompatBrain"]


class BrainError(RuntimeError):
    """HTTP / protocol failure from a sub-brain, classified for the runner."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        # 429 + 5xx are transient; auth and 4xx request shapes are not.
        self.retryable = status == 429 or (status is not None and status >= 500)
        super().__init__(message)


def _message_to_wire(message: ChatMessage) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role}
    if message.role == "assistant" and message.tool_calls:
        wire["content"] = message.content or None
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(dict(call.arguments)),
                },
            }
            for call in message.tool_calls
        ]
    elif message.role == "tool":
        wire["content"] = message.content or ""
        wire["tool_call_id"] = message.tool_call_id
    else:
        wire["content"] = message.content or ""
    return wire


def _extract_reasoning(message: Mapping[str, Any]) -> str | None:
    content = message.get("reasoning_content")
    if isinstance(content, str) and content:
        return content
    details = message.get("reasoning_details")
    if isinstance(details, list):
        parts: list[str] = [
            text
            for item in details
            if isinstance(item, Mapping) and isinstance((text := item.get("text")), str)
        ]
        if parts:
            return "".join(parts)
    return None


def _parse_tool_calls(message: Mapping[str, Any]) -> tuple[BrainToolCall, ...]:
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return ()
    calls: list[BrainToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments_raw = function.get("arguments")
        arguments: Mapping[str, JsonValue] = {}
        if isinstance(arguments_raw, str) and arguments_raw.strip():
            try:
                parsed = json.loads(arguments_raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, Mapping):
                arguments = parsed
        elif isinstance(arguments_raw, Mapping):
            arguments = arguments_raw
        call_id = item.get("id")
        calls.append(
            BrainToolCall(
                id=str(call_id) if call_id else f"call_{index}",
                name=name,
                arguments=arguments,
            )
        )
    return tuple(calls)


def _parse_usage(payload: Mapping[str, Any]) -> Usage:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return Usage()
    completion_details = usage.get("completion_tokens_details")
    reasoning = 0
    if isinstance(completion_details, Mapping):
        value = completion_details.get("reasoning_tokens")
        if isinstance(value, (int, float)):
            reasoning = int(value)
    return Usage(
        input_tokens=int(usage.get("prompt_tokens", 0) or 0),
        output_tokens=int(usage.get("completion_tokens", 0) or 0),
        reasoning_tokens=reasoning,
        total_tokens=int(usage.get("total_tokens", 0) or 0),
    )


class OpenAICompatBrain:
    def __init__(
        self,
        spec: BrainSpec,
        *,
        api_key: str,
        base_url: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = spec.name
        self.model = model
        self._spec = spec
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(180.0, connect=10.0),
        )

    def list_models(self) -> list[ModelInfo]:
        return list(self._spec.models)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, JsonValue]] = (),
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AssistantReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_wire(message) for message in messages],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as error:
            raise BrainError(
                "transport",
                f"{self.name}: request failed: {type(error).__name__}: {error}",
            ) from error
        if response.status_code != 200:
            detail = ""
            try:
                body = response.json()
                error_body = body.get("error") if isinstance(body, Mapping) else None
                if isinstance(error_body, Mapping):
                    detail = str(error_body.get("message") or "")
            except (ValueError, AttributeError):
                detail = response.text[:300]
            code = "auth" if response.status_code in (401, 403) else "http"
            raise BrainError(
                code,
                f"{self.name}: HTTP {response.status_code}: {detail or response.text[:300]}",
                status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as error:
            raise BrainError("protocol", f"{self.name}: non-JSON response") from error
        choices = body.get("choices") if isinstance(body, Mapping) else None
        if not isinstance(choices, list) or not choices:
            raise BrainError("protocol", f"{self.name}: response has no choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        if not isinstance(message, Mapping):
            raise BrainError("protocol", f"{self.name}: choice has no message")
        text = message.get("content")
        stop = choice.get("finish_reason") if isinstance(choice, Mapping) else None
        return AssistantReply(
            text=text if isinstance(text, str) else "",
            reasoning=_extract_reasoning(message),
            tool_calls=_parse_tool_calls(message),
            usage=_parse_usage(body),
            model=str(body.get("model") or self.model) if isinstance(body, Mapping) else self.model,
            stop_reason=str(stop) if stop else None,
        )
