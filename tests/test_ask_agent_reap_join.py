"""ask_agent's child-stop is structurally joined, not fire-and-forget
(kernel-invariant #5).

Two guarantees:
  - the teardown-join seam (`track_background` + `join_background_tasks`) awaits
    outstanding fire-and-forget tasks before the daemon tears down (so a parked
    child-stop can't be dropped mid-flight), and
  - the reap primitive (`_spawn_child_stop`) creates a tracked task that actually
    invokes `cancel_job`, and the bounded shielded await the finally uses does not
    return until the child-stop has completed — the "parent does not finish until
    child cancellation completes" invariant, at the reap-primitive level.
"""

from __future__ import annotations

import asyncio
import unittest

from salient_core.bus._delegation import _REAP_CHILD_TIMEOUT, _spawn_child_stop
from salient_core.daemon._tasks import (
    _BACKGROUND_TASKS,
    join_background_tasks,
    track_background,
)


class _SpyRunner:
    """Records cancel_job calls; an optional gate blocks the stop until released,
    to observe ordering."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.cancelled: list[int] = []
        self.completed: list[int] = []
        self._gate = gate

    async def cancel_job(self, job_id: int) -> bool:
        self.cancelled.append(job_id)
        if self._gate is not None:
            await self._gate.wait()
        self.completed.append(job_id)
        return True


class JoinBackgroundTasksTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_awaits_in_flight_tasks(self) -> None:
        done = asyncio.Event()

        async def work() -> None:
            await asyncio.sleep(0.02)
            done.set()

        track_background(asyncio.ensure_future(work()))
        await join_background_tasks(timeout=1.0)
        # The task completed before join returned (not dropped mid-flight).
        self.assertTrue(done.is_set())

    async def test_join_cancels_stragglers_past_deadline(self) -> None:
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.sleep(3600)

        task = track_background(asyncio.ensure_future(forever()))
        await started.wait()
        await join_background_tasks(timeout=0.05)  # deadline < task → cancel+reap
        self.assertTrue(task.cancelled() or task.done())
        self.assertNotIn(task, _BACKGROUND_TASKS)

    async def test_join_does_not_hang_on_a_task_that_suppresses_cancel(self) -> None:
        # A straggler that swallows CancelledError must not hang teardown forever
        # — the bounded straggler grace gives up and returns.
        import contextlib
        import unittest.mock as mock

        from salient_core.daemon import _tasks

        started = asyncio.Event()
        release = asyncio.Event()

        async def stubborn() -> None:
            started.set()
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    if release.is_set():
                        raise  # honour cancellation only once released
                    continue  # otherwise refuse it (the pathological case)

        task = track_background(asyncio.ensure_future(stubborn()))
        await started.wait()
        with mock.patch.object(_tasks, "_STRAGGLER_GRACE", 0.05):
            await asyncio.wait_for(join_background_tasks(timeout=0.02), timeout=2.0)
        # Returned promptly despite the stubborn task still running.
        self.assertFalse(task.done())
        # Cleanup: release so the task honours cancellation and the loop closes.
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_join_drains_tasks_registered_during_shutdown(self) -> None:
        # A task that, while shutting down, parks ANOTHER background task — the
        # loop-until-drained must catch the second one, not just the snapshot.
        second_done = asyncio.Event()

        async def second() -> None:
            await asyncio.sleep(0.02)
            second_done.set()

        async def first() -> None:
            await asyncio.sleep(0.02)
            track_background(asyncio.ensure_future(second()))

        track_background(asyncio.ensure_future(first()))
        await join_background_tasks(timeout=1.0)
        self.assertTrue(second_done.is_set())


class SpawnChildStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_child_stop_tracks_and_invokes_cancel(self) -> None:
        runner = _SpyRunner()
        task = _spawn_child_stop(runner, 55, "recon")
        # Registered for the teardown join immediately (before any await).
        self.assertIn(task, _BACKGROUND_TASKS)
        await task
        self.assertEqual(runner.cancelled, [55])

    async def test_bounded_await_blocks_until_child_stop_completes(self) -> None:
        # The finally's `await wait_for(shield(task), bound)` must not return
        # until cancel_job has actually completed. Gate the stop, confirm the
        # await is still pending, then release and confirm it completes.
        gate = asyncio.Event()
        runner = _SpyRunner(gate=gate)
        cancel_task = _spawn_child_stop(runner, 77, "recon")

        waiter = asyncio.ensure_future(
            asyncio.wait_for(asyncio.shield(cancel_task), _REAP_CHILD_TIMEOUT)
        )
        await asyncio.sleep(0.01)
        # cancel_job started but is blocked on the gate → the reap await is still
        # pending (the parent would NOT have returned yet).
        self.assertEqual(runner.cancelled, [77])
        self.assertEqual(runner.completed, [])
        self.assertFalse(waiter.done())

        gate.set()
        await waiter
        self.assertEqual(runner.completed, [77])

    async def test_shield_survives_waiter_timeout_for_teardown_join(self) -> None:
        # If the bounded await times out, the stop task survives (shielded) and
        # is still parked for the teardown join — never dropped.
        gate = asyncio.Event()
        runner = _SpyRunner(gate=gate)
        cancel_task = _spawn_child_stop(runner, 88, "recon")

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(cancel_task), 0.02)
        # Still alive + tracked for teardown.
        self.assertFalse(cancel_task.done())
        self.assertIn(cancel_task, _BACKGROUND_TASKS)

        gate.set()
        await join_background_tasks(timeout=1.0)
        self.assertEqual(runner.completed, [88])


if __name__ == "__main__":
    unittest.main()
