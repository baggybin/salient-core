from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from salient_core.daemon import AgentRunner, Job
from salient_core.runtime import (
    AgentEvent,
    AssistantEvent,
    TextContent,
    TurnCompletedEvent,
    TurnUsage,
)


class _NormalizedBackend:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.interrupted = False

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[AgentEvent]:
        yield AssistantEvent(
            content=(TextContent("hello without Claude messages"),),
            model="fake-model",
        )
        yield TurnCompletedEvent(
            turns=1,
            duration_ms=5,
            usage=TurnUsage(input_tokens=11, output_tokens=7, cost_usd=None),
        )

    async def interrupt(self) -> None:
        self.interrupted = True

    async def get_context_usage(self) -> None:
        return None

    def diagnose_failure(
        self,
        agent_name: str,
        error: BaseException,
        stderr_tail: tuple[str, ...],
    ) -> str:
        del stderr_tail
        return f"{agent_name}: {type(error).__name__}: {error}"


@pytest.mark.anyio
async def test_normalized_backend_completes_full_runner_turn() -> None:
    # Given: the real runner path and a fake backend with no Claude message objects.
    backend = _NormalizedBackend()
    runner = AgentRunner(
        name="normalized",
        cfg={},
        prompt_timeout=60.0,
        idle_timeout=0.0,
    )
    runner._backend = backend
    job = Job(id=1, prompt="hi", submitted_at=0.0)

    # When: the real processing path consumes the provider stream.
    await runner._process(job)

    # Then: output and normalized usage are observable on the runner.
    assert backend.queries
    assert job.result == "hello without Claude messages"
    assert runner.total_input_tokens == 11
    assert runner.total_output_tokens == 7
    assert runner.total_jobs_completed == 1
    done = next(event for event in runner.recent_events if event["kind"] == "done")
    assert done["meta"]["usage"]["cost_usd"] is None
    assert "cost=n/a" in done["text"]


@pytest.mark.anyio
async def test_interrupt_during_normalized_turn_reaches_backend() -> None:
    # Given: a runner with an active normalized backend turn.
    backend = _NormalizedBackend()
    runner = AgentRunner(name="normalized", cfg={})
    runner._backend = backend
    runner._turn_active = True
    runner.current = Job(id=2, prompt="wait", submitted_at=0.0)

    # When: the current job is cancelled.
    await runner.cancel_job(2)

    # Then: interruption crosses the provider-neutral lifecycle seam.
    assert backend.interrupted is True


class _ScriptedBackend(_NormalizedBackend):
    """Backend that emits a text reply (or nothing) then completes the turn."""

    def __init__(self, *, emit_text: bool) -> None:
        super().__init__()
        self._emit_text = emit_text

    async def receive_response(self) -> AsyncIterator[AgentEvent]:
        if self._emit_text:
            yield AssistantEvent(content=(TextContent("READY"),), model="fake")
        yield TurnCompletedEvent(
            turns=1, duration_ms=1, usage=TurnUsage(input_tokens=1, output_tokens=1, cost_usd=None)
        )


@pytest.mark.anyio
async def test_text_reply_to_awaiting_caller_is_not_re_prompted() -> None:
    # A delegated agent that answers in prose delivered its reply (job.result is
    # returned to the caller) — the silent-completion nudge must NOT fire, else the
    # reply is duplicated (the codex READYREADY symptom).
    import asyncio

    backend = _ScriptedBackend(emit_text=True)
    runner = AgentRunner(name="delegate", cfg={}, prompt_timeout=60.0, idle_timeout=0.0)
    runner._backend = backend
    job = Job(id=1, prompt="reply READY", submitted_at=0.0)
    job.future = asyncio.get_event_loop().create_future()  # a caller is awaiting

    await runner._process(job)

    assert len(backend.queries) == 1  # no nudge re-query
    assert job.result == "READY"


@pytest.mark.anyio
async def test_truly_silent_completion_to_awaiting_caller_is_re_prompted_once() -> None:
    # No text, no tool call, no <ask_operator> while a caller awaits → the agent
    # genuinely produced nothing, so the nudge fires exactly once.
    import asyncio

    backend = _ScriptedBackend(emit_text=False)
    runner = AgentRunner(name="delegate", cfg={}, prompt_timeout=60.0, idle_timeout=0.0)
    runner._backend = backend
    job = Job(id=2, prompt="do the thing", submitted_at=0.0)
    job.future = asyncio.get_event_loop().create_future()

    await runner._process(job)

    assert len(backend.queries) == 2  # original + one nudge
    assert "ended a turn with no tool calls" in backend.queries[1]


class _RunawayBackend(_NormalizedBackend):
    """Emits a long stream of assistant turns — a runaway agent that never
    delivers. Ends with a TurnCompletedEvent so the un-capped control case
    terminates normally."""

    def __init__(self, *, turns: int) -> None:
        super().__init__()
        self._turns = turns

    async def receive_response(self) -> AsyncIterator[AgentEvent]:
        for i in range(self._turns):
            yield AssistantEvent(content=(TextContent(f"step {i} "),), model="fake")
        yield TurnCompletedEvent(
            turns=self._turns,
            duration_ms=1,
            usage=TurnUsage(input_tokens=1, output_tokens=1, cost_usd=None),
        )


@pytest.mark.anyio
async def test_cfg_max_turns_caps_job_without_dispatch_hint() -> None:
    # Backends with no native turn enforcement (codex) rely on the runner's
    # hard cap; a job submitted without a max_turns_hint must fall back to the
    # agent's static cfg max_turns instead of running unbounded.
    backend = _RunawayBackend(turns=50)
    runner = AgentRunner(
        name="runaway",
        cfg={"max_turns": 3, "hard_cap_buffer": 0},
        prompt_timeout=60.0,
        idle_timeout=0.0,
    )
    runner._backend = backend
    job = Job(id=1, prompt="go", submitted_at=0.0)

    await runner._process(job)

    assert backend.interrupted is True
    assert "[PARTIAL: runner hard cap" in (job.result or "")
    assert "(budget was 3" in job.result


@pytest.mark.anyio
async def test_dispatch_hint_wins_over_cfg_max_turns() -> None:
    # A per-dispatch budget hint is the caller's explicit envelope — it takes
    # precedence over the agent's static cfg cap.
    backend = _RunawayBackend(turns=50)
    runner = AgentRunner(
        name="runaway",
        cfg={"max_turns": 1000, "hard_cap_buffer": 0},
        prompt_timeout=60.0,
        idle_timeout=0.0,
    )
    runner._backend = backend
    job = Job(id=2, prompt="go", submitted_at=0.0, max_turns_hint=2)

    await runner._process(job)

    assert backend.interrupted is True
    assert "(budget was 2" in (job.result or "")


@pytest.mark.anyio
async def test_no_cap_when_neither_hint_nor_cfg_max_turns() -> None:
    # Without a hint or a cfg max_turns the runner imposes no cap — the
    # backend's own completion ends the job untouched.
    backend = _RunawayBackend(turns=10)
    runner = AgentRunner(name="free", cfg={}, prompt_timeout=60.0, idle_timeout=0.0)
    runner._backend = backend
    job = Job(id=3, prompt="go", submitted_at=0.0)

    await runner._process(job)

    assert backend.interrupted is False
    assert "[PARTIAL" not in (job.result or "")


@pytest.mark.anyio
async def test_context_read_polling_does_not_trip_loop_detection() -> None:
    # Swarm workers poll `context_read` (a side-effect-free read) waiting for peers
    # to write shared findings — that is a wait pattern, not a stuck loop, and must
    # not spam the operator with "loop suspected" questions. `_read` is exempt.
    runner = AgentRunner(name="worker", cfg={}, prompt_timeout=60.0, idle_timeout=0.0)
    fired: list[tuple[str, int]] = []
    runner._on_loop_detected = lambda _r, tool, repeats, _h: fired.append((tool, repeats))

    args = {"agent": "osint-swarm", "key": "swarm:osint-swarm/findings"}
    for _ in range(8):  # well past the default threshold of 3
        await runner._check_loop("mcp__bus__osint__context_read", args)
    assert fired == []  # exempt read tool → never files a loop question

    # A non-exempt (mutating) tool repeated with identical args still fires — but
    # only ONCE, even across many repeats (the per-key cooldown kills the spam the
    # detector would otherwise emit every threshold-th repeat after clearing).
    for _ in range(15):
        await runner._check_loop("mcp__bus__osint__context_write", args)
    assert len(fired) == 1
    assert fired[0][0] == "mcp__bus__osint__context_write"

    # A DIFFERENT (tool, args) loop still surfaces — the cooldown is per-key.
    for _ in range(6):
        await runner._check_loop("mcp__bus__osint__context_write", {"agent": "x", "key": "y"})
    assert len(fired) == 2
