"""`submit()` must report a refusal on the Job it returns.

A refused submit never enqueues. Historically it signalled that ONLY by failing
the caller's future — which exists on the ``--wait`` path and nowhere else, so a
fire-and-forget caller got back a Job that looked queued and was not. The daemon
worked around it for the stopped case by re-checking the same conditions after
calling submit; when the budget-parked branch was added later, that mirrored
predicate didn't grow with it and the drop went silent again.

`job.error` is the one signal that covers both paths, so nothing downstream has
to re-derive the refusal.
"""

from __future__ import annotations

import asyncio

import pytest

from salient_core.daemon import AgentRunner


def _runner() -> AgentRunner:
    return AgentRunner(name="refuser", cfg={}, prompt_timeout=60.0, idle_timeout=0.0)


@pytest.mark.anyio
async def test_stopping_runner_reports_refusal_without_a_future() -> None:
    runner = _runner()
    runner._stop_requested = True

    job = runner.submit("work the loop will never see")

    assert job.error is not None
    assert "stopping" in job.error
    assert runner.queue.empty()


@pytest.mark.anyio
async def test_parked_runner_reports_refusal_without_a_future() -> None:
    runner = _runner()
    runner._budget_parked = True

    job = runner.submit("work the loop will never see")

    assert job.error is not None
    assert "budget-parked" in job.error
    assert runner.queue.empty()


@pytest.mark.anyio
async def test_refusal_still_fails_the_future_when_one_is_given() -> None:
    # The --wait path must keep raising; job.error is additive, not a swap.
    runner = _runner()
    runner._budget_parked = True
    fut: asyncio.Future = asyncio.get_running_loop().create_future()

    job = runner.submit("waited work", future=fut)

    with pytest.raises(RuntimeError, match="budget-parked"):
        await asyncio.wait_for(fut, timeout=1)
    assert job.error is not None


@pytest.mark.anyio
async def test_accepted_submit_carries_no_error_and_enqueues() -> None:
    # The benign path: nothing refuses, so nothing reports an error.
    runner = _runner()

    job = runner.submit("real work")

    assert job.error is None
    assert not runner.queue.empty()
