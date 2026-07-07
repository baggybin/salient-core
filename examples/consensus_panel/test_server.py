"""Consensus-panel demo: runner event contract + server endpoints.

The runner test pins the event sequence and that abort short-circuits a leg and
that convergence is scored by the real kernel helper. The server test drives the
Starlette app with the in-process TestClient — models endpoint, a full SSE run,
and abort.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from runner import Leg, MockPanelRunner


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, panel, aborts=None):
        events = []

        async def emit(e):
            events.append(e)

        run = MockPanelRunner(step_delay=0)
        await run.run("speed up repeated calls", panel, "claude-opus-4-8", emit, aborts or {})
        return events

    async def test_event_sequence_and_convergence(self):
        panel = [Leg("a", "claude-opus-4-8"), Leg("b", "claude-sonnet-5")]
        events = await self._collect(panel)
        kinds = [e.kind for e in events]
        self.assertEqual(kinds[0], "run_start")
        self.assertEqual(kinds[-1], "done")
        self.assertIn("leg_start", kinds)
        self.assertIn("trace", kinds)
        self.assertIn("answer", kinds)
        conv = next(e for e in events if e.kind == "convergence")
        # a and b both say "memoize" → high semantic agreement.
        self.assertIsNotNone(conv.data["semantic_score"])
        self.assertGreater(conv.data["semantic_score"], 0.5)
        self.assertTrue(conv.data["judge"].startswith("AGREE"))

    async def test_divergent_panel_scores_lower(self):
        # a (cache) vs c (parallelize) share fewer words → lower score.
        agree = await self._collect([Leg("a", "m"), Leg("b", "m")])
        mixed = await self._collect([Leg("a", "m"), Leg("c", "m")])
        sa = next(e for e in agree if e.kind == "convergence").data["semantic_score"]
        sc = next(e for e in mixed if e.kind == "convergence").data["semantic_score"]
        self.assertGreater(sa, sc)

    async def test_abort_short_circuits_leg(self):
        panel = [Leg("a", "m"), Leg("b", "m")]
        aborts = {"a": asyncio.Event(), "b": asyncio.Event()}
        aborts["a"].set()  # abort leg a before it starts stepping
        events = await self._collect(panel, aborts)
        a_events = [e for e in events if e.leg == "a"]
        self.assertTrue(any(e.kind == "aborted" for e in a_events))
        self.assertFalse(any(e.kind == "answer" for e in a_events))
        # b still answers, but convergence needs >=2 answers → judge is None here.
        conv = next(e for e in events if e.kind == "convergence")
        self.assertIsNone(conv.data["judge"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        import server

        # Zero out the stream delay so the SSE test runs fast.
        server.RUNNER = MockPanelRunner(step_delay=0)
        from starlette.testclient import TestClient

        self.server = server
        self.client = TestClient(server.app)

    def test_models_endpoint(self):
        r = self.client.get("/api/models")
        self.assertEqual(r.status_code, 200)
        ids = {m["id"] for m in r.json()["models"]}
        self.assertIn("claude-opus-4-8", ids)

    def test_start_requires_two_models(self):
        r = self.client.post("/api/consensus", json={"question": "q", "panel": [{"model": "x"}]})
        self.assertEqual(r.status_code, 400)

    def test_full_run_streams_convergence(self):
        start = self.client.post(
            "/api/consensus",
            json={
                "question": "speed it up",
                "panel": [{"id": "a", "model": "m1"}, {"id": "b", "model": "m2"}],
                "judge_model": "m1",
            },
        )
        self.assertEqual(start.status_code, 200)
        run_id = start.json()["run_id"]

        seen = []
        with self.client.stream("GET", f"/api/consensus/{run_id}/events") as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    payload = json.loads(line[6:])
                    if payload:
                        seen.append(payload.get("kind"))
                if "event: end" in line:
                    break
        self.assertIn("convergence", seen)
        self.assertEqual(seen[-1], "done")

    def _stream_kinds(self, run_id):
        seen = []
        with self.client.stream("GET", f"/api/consensus/{run_id}/events") as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    payload = json.loads(line[6:])
                    if payload:
                        seen.append(payload.get("kind"))
                if "event: end" in line:
                    break
        return seen

    def test_two_subscribers_each_see_full_stream(self):
        # Fan-out + history replay: a second tab on the same run must get the
        # complete event stream, not just whatever the first tab didn't steal.
        start = self.client.post(
            "/api/consensus",
            json={
                "question": "speed it up",
                "panel": [{"id": "a", "model": "m1"}, {"id": "b", "model": "m2"}],
            },
        )
        run_id = start.json()["run_id"]
        first = self._stream_kinds(run_id)
        second = self._stream_kinds(run_id)  # late joiner — pure history replay
        for seen in (first, second):
            self.assertIn("convergence", seen)
            self.assertEqual(seen[-1], "done")
        self.assertEqual(first, second)

    def test_finished_runs_evicted_past_cap(self):
        from unittest import mock

        def _start():
            return self.client.post(
                "/api/consensus",
                json={
                    "question": "q",
                    "panel": [{"id": "a", "model": "m"}, {"id": "b", "model": "m"}],
                },
            ).json()["run_id"]

        with mock.patch.object(self.server, "_RUNS_CAP", 2):
            ids = [_start() for _ in range(4)]
            # Drain each run so its task finishes and it becomes evictable.
            for rid in ids:
                if rid in self.server._RUNS:
                    self._stream_kinds(rid)
            _start()
            self.assertLessEqual(len(self.server._RUNS), 3)  # cap + the new run

    def test_abort_unknown_leg_404(self):
        start = self.client.post(
            "/api/consensus",
            json={"question": "q", "panel": [{"id": "a", "model": "m"}, {"id": "b", "model": "m"}]},
        ).json()
        r = self.client.post(f"/api/consensus/{start['run_id']}/abort", json={"leg": "zzz"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
