"""`AgentRunner.snapshot_jobs` — a non-destructive read of the current +
queued jobs for operator listing.

It must NOT mutate the queue (unlike a drain), must expose the `current`
job's `turn_active` / `interrupt_pending` flags (so an operator command can
classify a cancel outcome from a pre-call snapshot without touching
`cancel_job`'s bool contract), must skip non-Job sentinels in the queue, and
must report the steer-lane depth (those entries are not id-addressable jobs).
"""

from __future__ import annotations

import unittest


def _make_runner(name: str = "child"):
    from salient_core.daemon import AgentRunner

    return AgentRunner(
        name=name,
        cfg={},
        prompt_timeout=60.0,
        idle_timeout=0.0,
    )


class SnapshotJobsTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_runner(self):
        r = _make_runner()
        snap = r.snapshot_jobs()
        self.assertIsNone(snap["current"])
        self.assertEqual(snap["queued"], [])
        self.assertEqual(snap["steer_lane_depth"], 0)

    async def test_queued_listed_in_order_non_destructively(self):
        from salient_core.daemon._helpers import Job
        from salient_core.daemon.runner import _STEER_WAKE

        r = _make_runner()
        r.queue.put_nowait(Job(id=1, prompt="first\nline", submitted_at=0.0))
        r.queue.put_nowait(_STEER_WAKE)  # non-Job sentinel — must be skipped
        r.queue.put_nowait(Job(id=2, prompt="second", submitted_at=0.0))

        snap = r.snapshot_jobs()
        self.assertEqual([j["id"] for j in snap["queued"]], [1, 2])
        # newlines flattened in the preview
        self.assertEqual(snap["queued"][0]["prompt_preview"], "first line")
        # the read did not drain the queue
        self.assertEqual(r.queue.qsize(), 3)

    async def test_current_flags_exposed(self):
        from salient_core.daemon._helpers import Job

        r = _make_runner()
        r.current = Job(id=10, prompt="running", submitted_at=0.0)
        r._turn_active = True
        snap = r.snapshot_jobs()
        self.assertEqual(snap["current"]["id"], 10)
        self.assertTrue(snap["current"]["turn_active"])
        self.assertFalse(snap["current"]["interrupt_pending"])

        # once the interrupt guard is set for this job, the snapshot reflects it
        r._interrupted_job_id = 10
        self.assertTrue(r.snapshot_jobs()["current"]["interrupt_pending"])

    async def test_steer_lane_depth_reported(self):
        r = _make_runner()
        r._steer_lane.append(object())
        r._steer_lane.append(object())
        self.assertEqual(r.snapshot_jobs()["steer_lane_depth"], 2)

    async def test_correlation_id_exposed_for_reconstruct(self):
        # T3.1: the snapshot surfaces each job's correlation_id so an operator
        # can feed it to `reconstruct` (current in-flight turn + queued).
        from salient_core.daemon._helpers import Job

        r = _make_runner()
        r.current = Job(id=10, prompt="running", submitted_at=0.0, correlation_id="op:1:10")
        r.queue.put_nowait(Job(id=11, prompt="next", submitted_at=0.0, correlation_id="op:1:11"))
        snap = r.snapshot_jobs()
        self.assertEqual(snap["current"]["correlation_id"], "op:1:10")
        self.assertEqual(snap["queued"][0]["correlation_id"], "op:1:11")


if __name__ == "__main__":
    unittest.main()
