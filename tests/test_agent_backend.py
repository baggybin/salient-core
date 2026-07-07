"""AgentBackend seam — the runner drives an AgentBackend, not ClaudeSDKClient.

Proves the decoupling three ways: LocalClaudeBackend satisfies the
(runtime_checkable) Protocol and exposes the raw client; the runner constructs
its backend via the injectable `backend_factory` on (re)connect; and a backend
that is NOT a ClaudeSDKClient can drive a full turn through `runner._process`.
"""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from salient_core.daemon import AgentRunner, Job
from salient_core.daemon._backend import LocalClaudeBackend
from salient_core.protocols import AgentBackend


class _FakeBackend:
    """A complete AgentBackend that is NOT a ClaudeSDKClient — records calls and
    yields a scripted `receive_response` stream."""

    def __init__(self, options: Any = None, messages: list | None = None) -> None:
        self.options = options
        self._messages = messages or []
        self.connected = False
        self.disconnected = False
        self.query_calls: list[str] = []
        self.interrupt_called = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str) -> None:
        self.query_calls.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for m in self._messages:
            yield m

    async def interrupt(self) -> None:
        self.interrupt_called = True

    async def get_context_usage(self) -> dict[str, Any] | None:
        return {"percentage": 10}

    @property
    def raw(self) -> Any:
        return self


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="backend-test")


def _runner(**kw: Any) -> AgentRunner:
    return AgentRunner(
        name="backend_test",
        cfg={},
        options=ClaudeAgentOptions(),
        prompt_timeout=60.0,
        idle_timeout=0.0,
        **kw,
    )


class AgentBackendProtocolTests(unittest.TestCase):
    def test_local_backend_satisfies_protocol(self):
        b = LocalClaudeBackend(ClaudeAgentOptions())
        self.assertIsInstance(b, AgentBackend)  # runtime_checkable
        self.assertIsInstance(b.raw, ClaudeSDKClient)  # exposes the underlying client

    def test_fake_backend_satisfies_protocol(self):
        self.assertIsInstance(_FakeBackend(), AgentBackend)

    def test_default_factory_is_local_claude_backend(self):
        self.assertIs(
            AgentRunner.__dataclass_fields__["backend_factory"].default,
            LocalClaudeBackend,
        )


class AgentBackendSeamTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_builds_and_connects_on_reconnect(self):
        # The runner builds its backend via backend_factory — inject a fake so
        # no ClaudeSDKClient subprocess is ever spawned.
        built: list[_FakeBackend] = []

        def factory(options):
            b = _FakeBackend(options)
            built.append(b)
            return b

        r = _runner(backend_factory=factory)
        await r._reconnect_client()
        self.assertEqual(len(built), 1)
        self.assertIs(r._backend, built[0])
        self.assertTrue(built[0].connected)

    async def test_non_claude_backend_drives_a_turn(self):
        # A backend that is NOT a ClaudeSDKClient drives a full turn: the runner
        # depends only on the AgentBackend surface.
        r = _runner()
        fake = _FakeBackend(messages=[_assistant("hello from a non-Claude backend")])
        r._backend = fake
        job = Job(id=1, prompt="hi", submitted_at=0.0)
        await r._process(job)
        self.assertTrue(fake.query_calls, "the turn must have been dispatched via query()")
        self.assertIn("hello from a non-Claude backend", job.result or "")
        self.assertIsNone(job.error)


if __name__ == "__main__":
    unittest.main()
