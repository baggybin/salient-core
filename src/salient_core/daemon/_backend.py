"""Default agent backend — a 1:1 passthrough over a local `ClaudeSDKClient`.

The `AgentBackend` Protocol lives in `salient_core/protocols.py` (the kernel's
interface surface); this module ships the concrete local implementation the
runner uses by default. A future `RemoteBackend` (a runner proxied over a socket
to another process/host) is a separate implementation of the same Protocol — the
local path stays direct/in-process, so only genuinely remote calls would ever
pay serialization.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


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

    def receive_response(self) -> AsyncIterator[Any]:
        return self._client.receive_response()

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def get_context_usage(self) -> dict[str, Any] | None:
        usage = await self._client.get_context_usage()
        # ContextUsageResponse is a TypedDict (dict subclass); copy to a plain
        # dict so the AgentBackend contract stays SDK-agnostic.
        return dict(usage) if usage is not None else None

    @property
    def raw(self) -> Any:
        return self._client
