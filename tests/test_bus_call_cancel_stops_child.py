"""`bus_call_cancel` must also STOP the child runner (cancel_job), not only
settle the caller's future.

Before: cancelling a leaked/stalled bus call unblocked the caller's await but
left the child runner burning tokens until its own timeout. Now, when the call
reached Phase 3 dispatch (child_job_id recorded), cancel schedules
`runner.cancel_job` for the child — and the existing swarm cascade carries each
child's own child_job_id, so cancelling a swarm parent stops every child.

Uses a `_QuestionsMixin` shim (same approach as test_bus_call_reaper.py) so the
production `bus_call_cancel` runs unchanged.
"""

from __future__ import annotations

import asyncio
import unittest

from salient_core.daemon._helpers import BusCall
from salient_core.daemon._questions import _QuestionsMixin


class _SpyRunner:
    def __init__(self, name: str) -> None:
        self.name = name
        self.cancelled: list[int] = []

    async def cancel_job(self, job_id: int) -> bool:
        self.cancelled.append(job_id)
        return True


class _CancelDaemon(_QuestionsMixin):
    """Minimal shim: `bus_call_cancel` only touches `_bus_calls`, `runners`,
    and `_clear_stall_question` (a no-op when stall_qid is None)."""

    def __init__(self) -> None:
        self._bus_calls: dict[int, BusCall] = {}
        self.runners: dict = {}


def _call(
    call_id, caller, target, *, child_job_id=None, parent_call_id=None, swarm_role=None
) -> BusCall:
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    c = BusCall(
        id=call_id,
        caller=caller,
        target=target,
        prompt_preview="x",
        started_at=0.0,
        state="awaiting_reply",
        future=fut,
        parent_call_id=parent_call_id,
        swarm_role=swarm_role,
    )
    c.child_job_id = child_job_id
    return c


class BusCallCancelStopsChildTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_settles_future_and_stops_child(self):
        d = _CancelDaemon()
        runner = _SpyRunner("recon")
        d.runners["recon"] = runner
        call = _call(1, "manager", "recon", child_job_id=55)
        d._bus_calls[1] = call

        cancelled = d.bus_call_cancel(call_id=1)

        self.assertEqual(cancelled, [1])
        # The future settles SYNCHRONOUSLY — the caller's await unblocks at
        # once; the child-stop is fire-and-forget so interrupt latency can't
        # delay the unblock.
        self.assertTrue(call.future.done())
        with self.assertRaises(RuntimeError):
            call.future.result()
        await asyncio.sleep(0.01)  # let the create_task'd cancel_job run
        self.assertEqual(runner.cancelled, [55])

    async def test_no_child_job_id_means_no_cancel_job(self):
        d = _CancelDaemon()
        runner = _SpyRunner("recon")
        d.runners["recon"] = runner
        # Never dispatched (denied at a gate) → child_job_id stays None.
        d._bus_calls[1] = _call(1, "manager", "recon", child_job_id=None)

        d.bus_call_cancel(call_id=1)

        await asyncio.sleep(0.01)
        self.assertEqual(
            runner.cancelled,
            [],
            "no dispatch happened — there's no child job to stop.",
        )

    async def test_swarm_cascade_stops_all_children(self):
        d = _CancelDaemon()
        r1, r2 = _SpyRunner("c1"), _SpyRunner("c2")
        d.runners["c1"], d.runners["c2"] = r1, r2
        d._bus_calls = {
            1: _call(1, "manager", "manager", swarm_role="parent"),
            2: _call(2, "manager", "c1", child_job_id=21, parent_call_id=1),
            3: _call(3, "manager", "c2", child_job_id=22, parent_call_id=1),
        }

        cancelled = d.bus_call_cancel(call_id=1)

        self.assertEqual(set(cancelled), {1, 2, 3})
        await asyncio.sleep(0.01)
        self.assertEqual(r1.cancelled, [21])
        self.assertEqual(r2.cancelled, [22])


if __name__ == "__main__":
    unittest.main()
