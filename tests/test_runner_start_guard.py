"""Runner start()-idempotency + status-vocabulary pins (core TOCTOU / H5).

A second ``start()`` on a live runner used to spawn a DUPLICATE ``_run`` loop on
the same queue/backend (two loops split the stream, double-spend, and when one
exits its ``finally`` sets ``status="stopped"`` while the other still serves). And
the status vocabulary is now exported as data (``LIVE_STATUSES``) so an app guard
tests membership instead of inventing a literal like ``"running"`` the kernel
never emits.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from unittest import mock

import pytest

from salient_core.daemon import runner as runner_mod
from salient_core.daemon.runner import (
    LIVE_STATUSES,
    RUNNER_STATUSES,
    TERMINAL_STATUSES,
    AgentRunner,
)


def _runner() -> AgentRunner:
    return AgentRunner(name="x", cfg={}, prompt_timeout=60.0, idle_timeout=0.0)


@pytest.mark.anyio
async def test_start_is_idempotent_while_running() -> None:
    r = _runner()

    async def _live() -> None:
        await asyncio.sleep(3600)

    r._task = asyncio.create_task(_live())
    try:
        first = r._task
        await r.start()  # a live loop already runs → must be a no-op
        assert r._task is first, "start() must not spawn a second _run on a live runner"
    finally:
        r._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await r._task


@pytest.mark.anyio
async def test_start_creates_task_when_absent() -> None:
    r = _runner()
    ran = asyncio.Event()

    async def _fake_run() -> None:
        ran.set()

    with mock.patch.object(r, "_run", _fake_run):
        await r.start()
        assert r._task is not None
        await asyncio.wait_for(ran.wait(), timeout=1.0)


def test_status_vocabulary_partitions_and_is_exhaustive() -> None:
    # LIVE and TERMINAL partition the vocabulary with no overlap.
    assert LIVE_STATUSES.isdisjoint(TERMINAL_STATUSES)
    assert RUNNER_STATUSES == LIVE_STATUSES | TERMINAL_STATUSES
    # Source-scan: every status the runner ASSIGNS must be classified, so a new
    # status can't ship unclassified (drift guard, like the action-class pin).
    src = Path(runner_mod.__file__).read_text()
    assigned = set(re.findall(r'self\.status = "([a-z_]+)"', src))
    assigned.add("starting")  # the field default: `status: str = "starting"`
    missing = assigned - RUNNER_STATUSES
    assert not missing, f"runner assigns statuses not in RUNNER_STATUSES: {missing}"


if __name__ == "__main__":
    import unittest

    unittest.main()
