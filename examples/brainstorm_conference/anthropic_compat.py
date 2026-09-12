"""A tiny httpx client for Anthropic's Messages API — the Claude seat.

The kernel env ships no ``anthropic`` SDK, and we only need one non-streaming
request/reply, so this is a ~1-screen client in the same shape as the kernel's
``OpenAICompatBrain`` (``.chat(messages=…) -> reply.text`` + ``.aclose()``), so a
Claude panelist is a drop-in on the same ``Panelist``.

Auth covers both paths:
* ``ANTHROPIC_API_KEY`` → the standard ``x-api-key`` header.
* ``ANTHROPIC_AUTH_TOKEN`` (+ optional ``ANTHROPIC_BASE_URL``) → a bearer token,
  which is how fable/opus reach an OAuth/proxy endpoint (the same override the
  kernel's own endpoint path uses).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from salient_core.polybrain.types import ChatMessage

_ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True, slots=True)
class Reply:
    """Minimal reply — only ``.text`` is consumed by the panelist."""

    text: str


class AnthropicError(RuntimeError):
    pass


class AnthropicBrain:
    """Non-streaming Messages-API client. System messages are hoisted into the
    top-level ``system`` field; user/assistant messages pass through."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        auth_style: str = "api_key",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = "anthropic"
        self.model = model
        if auth_style == "bearer":
            self._headers = {"authorization": f"Bearer {api_key}"}
        else:
            self._headers = {"x-api-key": api_key}
        self._headers["anthropic-version"] = _ANTHROPIC_VERSION
        # Headers are applied per-request (below), not baked into the client, so an
        # injected client (tests) still sends auth + version.
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(180.0, connect=10.0),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float | None = None,
        max_tokens: int = 8192,
    ) -> Reply:
        system = "\n\n".join(
            (m.content or "") for m in messages if m.role == "system"
        ).strip()
        turns = [
            {"role": m.role, "content": m.content or ""}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": turns,
        }
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        try:
            response = await self._client.post(
                "/v1/messages", json=payload, headers=self._headers
            )
        except httpx.HTTPError as error:
            raise AnthropicError(
                f"anthropic: request failed: {type(error).__name__}: {error}"
            ) from error
        if response.status_code != 200:
            raise AnthropicError(
                f"anthropic: HTTP {response.status_code}: {response.text[:300]}"
            )
        body = response.json()
        blocks = body.get("content") if isinstance(body, dict) else None
        if not isinstance(blocks, list):
            raise AnthropicError("anthropic: response has no content blocks")
        text = "".join(
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        return Reply(text=text)
