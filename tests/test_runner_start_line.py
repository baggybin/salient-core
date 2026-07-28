"""The operator-facing `agent <name> started` line must fire for EVERY start
path, not just the operator-issued one.

It used to live at the end of ``AgentFactory.start_agent`` — one of ~13 call
sites that build a runner and call ``start()``. Boot / resume / autostart build
runners directly (the skin's ``Daemon._start_initial_agents``), so the agents
that live longest and do the most work were exactly the ones that never logged a
start: a real archive held 44 start lines against 2,374 stops. These tests drive
the runner the way those paths do — no factory involved.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from salient_core.daemon import AgentRunner
from salient_core.runtime import AgentEvent

_RUNNER_LOGGER = "salient.daemon.runner"


class _IdleBackend:
    """Connects, then never produces anything — enough to reach 'ready'."""

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        del prompt

    async def receive_response(self) -> AsyncIterator[AgentEvent]:
        return
        yield  # pragma: no cover — makes this an async generator

    async def interrupt(self) -> None:
        return None

    async def get_context_usage(self) -> None:
        return None


async def _run_until_ready(runner: AgentRunner) -> None:
    """Start the runner's own task and stop it once it reports ready.

    Mirrors the boot/resume path: construct, ``start()``, no ``start_agent``.
    """
    await runner.start()
    for _ in range(200):
        if runner.status == "idle":
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover — a hung connect is a real failure
        pytest.fail("runner never reached ready")
    runner._stop_requested = True
    await runner.queue.put(None)  # shutdown sentinel
    task = runner._task
    if task is not None:
        await asyncio.wait_for(task, timeout=5)


def _make_runner(monkeypatch: pytest.MonkeyPatch, *, model: str | None) -> AgentRunner:
    cfg = {"model": model} if model is not None else {}
    runner = AgentRunner(name="resumed", cfg=cfg, prompt_timeout=60.0, idle_timeout=0.0)
    monkeypatch.setattr(runner, "_create_backend", lambda: _IdleBackend())
    monkeypatch.setattr(runner, "_connect_with_attach_retry", _IdleBackend().connect)
    return runner


@pytest.mark.anyio
async def test_start_line_fires_without_start_agent(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: a runner built and started directly, as boot/resume does.
    runner = _make_runner(monkeypatch, model="haiku")

    # When: it comes up.
    with caplog.at_level(logging.INFO, logger=_RUNNER_LOGGER):
        await _run_until_ready(runner)

    # Then: the start line is logged exactly once, naming the configured brain.
    starts = [r for r in caplog.records if "started" in r.getMessage()]
    assert len(starts) == 1, [r.getMessage() for r in starts]
    assert starts[0].getMessage() == "agent resumed started model=haiku"
    # …under the same logger as the stop line, so both reach daemon.log together.
    assert starts[0].name == _RUNNER_LOGGER


@pytest.mark.anyio
async def test_start_line_survives_a_config_without_a_model(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An agent may inherit its brain (shadows, endpoint agents, forks) and carry
    # no explicit `model:`. The line must still fire — a missing model is not a
    # reason to lose the provenance anchor entirely.
    runner = _make_runner(monkeypatch, model=None)

    with caplog.at_level(logging.INFO, logger=_RUNNER_LOGGER):
        await _run_until_ready(runner)

    starts = [r for r in caplog.records if "started" in r.getMessage()]
    assert len(starts) == 1
    assert starts[0].getMessage() == "agent resumed started model=None"


@pytest.mark.anyio
async def test_start_and_stop_lines_are_symmetric(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The bug was an asymmetry: stops logged unconditionally from the runner,
    # starts only from the factory. One start, one stop — from one logger.
    runner = _make_runner(monkeypatch, model="sonnet")

    with caplog.at_level(logging.INFO, logger=_RUNNER_LOGGER):
        await _run_until_ready(runner)
        await runner.stop()

    messages = [r.getMessage() for r in caplog.records if r.name == _RUNNER_LOGGER]
    assert sum("started" in m for m in messages) == 1
    assert sum("stopped" in m for m in messages) == 1
