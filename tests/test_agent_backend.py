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
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from salient_core.daemon import AgentRunner, Job
from salient_core.daemon._backend import LocalClaudeBackend
from salient_core.protocols import AgentBackend
from salient_core.runtime import (
    AssistantEvent,
    ContextUsage,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolResultEvent,
    TurnCompletedEvent,
)


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

    async def get_context_usage(self) -> ContextUsage | None:
        return ContextUsage(used_tokens=10, max_tokens=100, percentage=10.0)

    def diagnose_failure(
        self,
        agent_name: str,
        error: BaseException,
        stderr_tail: tuple[str, ...],
    ) -> str:
        del stderr_tail
        return f"{agent_name}: {type(error).__name__}: {error}"


def _assistant(text: str) -> AssistantEvent:
    return AssistantEvent(content=(TextContent(text=text),), model="backend-test")


def _runner(**kw: Any) -> AgentRunner:
    return AgentRunner(
        name="backend_test",
        cfg={},
        prompt_timeout=60.0,
        idle_timeout=0.0,
        **kw,
    )


class AgentBackendProtocolTests(unittest.TestCase):
    def test_local_backend_satisfies_protocol(self):
        b = LocalClaudeBackend(ClaudeAgentOptions())
        self.assertIsInstance(b, AgentBackend)  # runtime_checkable

    def test_fake_backend_satisfies_protocol(self):
        self.assertIsInstance(_FakeBackend(), AgentBackend)

    def test_runner_requires_an_explicit_backend_factory(self):
        with self.assertRaisesRegex(RuntimeError, "no agent backend configured"):
            _runner()._create_backend()


class AgentBackendSeamTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_adapter_preserves_stream_content_and_usage(self):
        messages = [
            AssistantMessage(
                content=[
                    TextBlock(text="hello"),
                    ThinkingBlock(thinking="reason", signature="sig"),
                    ToolUseBlock(id="call-1", name="echo", input={"text": "hi"}),
                ],
                model="claude-test",
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="call-1", content="done")]),
            ResultMessage(
                subtype="success",
                duration_ms=12,
                duration_api_ms=10,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.25,
                usage={"input_tokens": 3, "output_tokens": 4},
            ),
        ]

        class ScriptedClient:
            async def receive_response(self):
                for message in messages:
                    yield message

        backend = LocalClaudeBackend(ClaudeAgentOptions())
        backend._client = ScriptedClient()
        events = [event async for event in backend.receive_response()]

        assistant = events[0]
        self.assertIsInstance(assistant, AssistantEvent)
        assert isinstance(assistant, AssistantEvent)
        self.assertEqual(assistant.content[0], TextContent("hello"))
        self.assertEqual(assistant.content[1], ThinkingContent("reason"))
        self.assertEqual(
            assistant.content[2],
            ToolCallContent("call-1", "echo", {"text": "hi"}),
        )
        self.assertEqual(events[1], ToolResultEvent("call-1", "done"))
        self.assertEqual(
            events[2],
            TurnCompletedEvent(
                turns=1,
                duration_ms=12,
                usage=events[2].usage,
            ),
        )
        assert isinstance(events[2], TurnCompletedEvent)
        self.assertEqual(events[2].usage.input_tokens, 3)
        self.assertEqual(events[2].usage.output_tokens, 4)
        # A lone turn's per-turn cost equals the cumulative (baseline 0.0).
        self.assertEqual(events[2].usage.cost_usd, 0.25)

    async def test_cost_usd_is_per_turn_not_session_cumulative(self):
        # The SDK's `total_cost_usd` is a SESSION-CUMULATIVE running total, but
        # every consumer sums `TurnUsage.cost_usd` per turn. The backend must
        # emit the per-turn delta so those sums are correct — else reported cost
        # inflates cumulative-on-cumulative. Regression for the ledger audit's
        # real (audit-missed) finding: cost over-summed ~2.5-3.4x.
        backend = LocalClaudeBackend(ClaudeAgentOptions())

        async def _one_turn(cumulative: float) -> TurnCompletedEvent:
            # Each job runs its own receive_response() stream over the SAME
            # backend instance (one connected session) — mirrors the runner.
            class _Client:
                async def receive_response(self):
                    yield ResultMessage(
                        subtype="success",
                        duration_ms=1,
                        duration_api_ms=1,
                        is_error=False,
                        num_turns=1,
                        session_id="s",
                        total_cost_usd=cumulative,
                        usage={"input_tokens": 1, "output_tokens": 1},
                    )

            backend._client = _Client()
            events = [e async for e in backend.receive_response()]
            assert isinstance(events[0], TurnCompletedEvent)
            return events[0]

        # Cumulative rises within the session → emitted cost is the per-turn delta.
        self.assertAlmostEqual((await _one_turn(0.677)).usage.cost_usd, 0.677)
        self.assertAlmostEqual((await _one_turn(0.941)).usage.cost_usd, 0.264)
        self.assertAlmostEqual((await _one_turn(1.047)).usage.cost_usd, 0.106)
        # Summing the per-turn costs recovers the true final cumulative — the
        # property every downstream `sum(cost_usd)` relies on.
        self.assertAlmostEqual(0.677 + 0.264 + 0.106, 1.047)
        # A drop below the last seen cumulative = the session's cost was reset
        # (reconnect / new epoch). Re-baseline: emit the new value, never a
        # negative delta.
        self.assertAlmostEqual((await _one_turn(0.798)).usage.cost_usd, 0.798)
        self.assertAlmostEqual((await _one_turn(1.075)).usage.cost_usd, 0.277)

    async def test_cost_usd_none_is_passed_through(self):
        # An endpoint/provider that reports no dollar cost must yield None, not 0.0,
        # and must not disturb the per-turn delta baseline.
        backend = LocalClaudeBackend(ClaudeAgentOptions())

        class _Client:
            async def receive_response(self):
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    total_cost_usd=None,
                    usage={"input_tokens": 1, "output_tokens": 1},
                )

        backend._client = _Client()
        events = [e async for e in backend.receive_response()]
        assert isinstance(events[0], TurnCompletedEvent)
        self.assertIsNone(events[0].usage.cost_usd)

    async def test_factory_builds_and_connects_on_reconnect(self):
        # The runner builds its backend via backend_factory — inject a fake so
        # no ClaudeSDKClient subprocess is ever spawned.
        built: list[_FakeBackend] = []

        def factory():
            b = _FakeBackend()
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
