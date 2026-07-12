"""Piece B: `AgentRunner.cancel_job` stops one specific job.

Queued → dropped from the FIFO (non-Job sentinels + order preserved), its
future settled so the caller's await never leaks. In-flight → interrupt the
SDK turn, but ONLY when a turn is actually streaming (`_turn_active`), so a
cancel in the post-turn finalization window can't fire a spurious interrupt.
Wired into `bus_call_cancel` so a cancelled delegation also stops the child
burning tokens (see test_bus_call_cancel_stops_child.py).
"""

from __future__ import annotations

import asyncio
import unittest


def _make_runner(name: str = "child"):
    from salient_core.daemon import AgentRunner

    return AgentRunner(
        name=name,
        cfg={},
        prompt_timeout=60.0,
        idle_timeout=0.0,
    )


class _SpyClient:
    def __init__(self) -> None:
        self.interrupts = 0

    async def interrupt(self) -> None:
        self.interrupts += 1


class CancelJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_job_dropped_others_preserved(self):
        from salient_core.daemon._helpers import Job

        r = _make_runner()
        for jid in (1, 2, 3):
            r.queue.put_nowait(Job(id=jid, prompt="x", submitted_at=0.0))
        self.assertTrue(await r.cancel_job(2))
        remaining = []
        while not r.queue.empty():
            remaining.append(r.queue.get_nowait())
        self.assertEqual(
            [j.id for j in remaining],
            [1, 3],
            "only the matching job is dropped; FIFO order of the rest holds.",
        )

    async def test_non_job_sentinels_preserved(self):
        from salient_core.daemon._helpers import Job
        from salient_core.daemon.runner import _STEER_WAKE

        r = _make_runner()
        r.queue.put_nowait(_STEER_WAKE)
        r.queue.put_nowait(Job(id=5, prompt="x", submitted_at=0.0))
        r.queue.put_nowait(None)  # shutdown sentinel
        self.assertTrue(await r.cancel_job(5))
        remaining = []
        while not r.queue.empty():
            remaining.append(r.queue.get_nowait())
        self.assertEqual(
            remaining,
            [_STEER_WAKE, None],
            "the drain must preserve non-Job sentinels (steer-wake / shutdown) "
            "in order — they're not jobs and aren't ours to drop.",
        )

    async def test_missing_job_returns_false(self):
        r = _make_runner()
        self.assertFalse(await r.cancel_job(999))

    async def test_dropped_queued_job_future_settled(self):
        from salient_core.daemon._helpers import Job

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        r = _make_runner()
        r.queue.put_nowait(Job(id=8, prompt="x", submitted_at=0.0, future=fut))
        await r.cancel_job(8)
        self.assertTrue(fut.done())
        with self.assertRaises(RuntimeError):
            fut.result()

    async def test_inflight_job_interrupted(self):
        from salient_core.daemon._helpers import Job

        r = _make_runner()
        spy = _SpyClient()
        r._backend = spy
        r._turn_active = True
        r.current = Job(id=10, prompt="x", submitted_at=0.0)
        self.assertTrue(await r.cancel_job(10))
        self.assertEqual(spy.interrupts, 1)
        self.assertIn("JOB CANCELLED", r._last_interrupt_reason or "")

    async def test_inflight_no_interrupt_when_turn_inactive(self):
        from salient_core.daemon._helpers import Job

        r = _make_runner()
        spy = _SpyClient()
        r._backend = spy
        r._turn_active = False  # post-turn finalization window
        r.current = Job(id=11, prompt="x", submitted_at=0.0)
        self.assertTrue(
            await r.cancel_job(11),
            "still the current job → found=True even between turns.",
        )
        self.assertEqual(
            spy.interrupts,
            0,
            "no turn is streaming — interrupting now would be spurious (same "
            "gate as steer(interrupt=True)).",
        )


if __name__ == "__main__":
    unittest.main()
