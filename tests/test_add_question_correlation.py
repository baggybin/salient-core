"""T3.1 spine — the daemon `add_question` path threads correlation_id.

Regression pin for a silent forensic-join drop: `record_question` and
`build_reconstruction` both already handled correlation_id, and the existing
reconstruct tests seeded questions *with* a cid — so they stayed green while the
LIVE path (`_QuestionsMixin.add_question`, used by ask_operator / bus-stall /
delegation) resolved the runner's current job for `job_id` but dropped its
correlation. Result: every operator question persisted with an empty
correlation_id and never joined into its turn's reconstruction.

This exercises `add_question` itself and asserts BOTH integration points
(DeepSeek): the written column AND that the reconstruction includes the row.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from salient_core.bus import ContextStore
from salient_core.coord.questions import QuestionInbox
from salient_core.coord.reconstruct import build_reconstruction
from salient_core.daemon._questions import _QuestionsMixin
from salient_core.policy.scope import ScopeStore


class _FakeJob:
    """Stand-in for the runner's in-flight Job — only the fields add_question
    reads (`id`, `correlation_id`) plus the list it appends to."""

    def __init__(self, job_id: int, correlation_id: str) -> None:
        self.id = job_id
        self.correlation_id = correlation_id
        self.tool_question_ids: list[int] = []


class _FakeRunner:
    def __init__(self, current: _FakeJob | None) -> None:
        self.current = current

    def _publish(self, kind: str, body: str) -> None:  # _announce_question hook
        pass


class _Harness(_QuestionsMixin):
    """Minimal object carrying just what add_question / _announce_question
    touch — skips the daemon __init__ chain on purpose."""

    def __init__(self, inbox: QuestionInbox, runners: dict[str, Any]) -> None:
        self.inbox = inbox
        self.runners = runners


class AddQuestionCorrelationTests(unittest.TestCase):
    def _wire(self, td: str, current: _FakeJob | None):
        ctx = ContextStore(Path(td) / "state.db", engagement_id="op")
        scope = ScopeStore(Path(td) / "scope.db", "op")
        inbox = QuestionInbox(ctx)
        runner = _FakeRunner(current)
        harness = _Harness(inbox, {"scout": runner})
        return ctx, scope, harness, runner

    def test_question_inherits_current_turn_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cid = "op:2:5"
            job = _FakeJob(job_id=1, correlation_id=cid)
            ctx, scope, harness, runner = self._wire(td, job)
            try:
                # The turn's job anchor (so the reconstruction has a chain head).
                ctx.record_job(
                    agent="scout",
                    job_id=1,
                    prompt="do work",
                    submitted_at=1.0,
                    started_at=1.0,
                    finished_at=None,
                    result="",
                    error=None,
                    correlation_id=cid,
                    assembled_prompt_sha="sha",
                )

                qid = harness.add_question("scout", "proceed with the exploit path?")

                # (1) write path — the persisted row carries the turn's cid.
                pend = ctx.load_pending_questions()
                row = next(r for r in pend if r["id"] == qid)
                self.assertEqual(row["correlation_id"], cid)

                # (2) read/join path — reconstruction actually includes it.
                r = build_reconstruction(ctx, scope, cid)
                self.assertTrue(r["found"])
                self.assertEqual(len(r["questions"]), 1)
                self.assertEqual(r["questions"][0]["id"], qid)

                # original behavior preserved: id linked onto the live job.
                self.assertIn(qid, runner.current.tool_question_ids)
            finally:
                ctx.close()
                scope.close()

    def test_idle_agent_gets_no_fabricated_correlation(self) -> None:
        # No current turn ⇒ correlation stays None (honest "not attributable"),
        # never back-filled to a stale turn.
        with tempfile.TemporaryDirectory() as td:
            ctx, scope, harness, _ = self._wire(td, current=None)
            try:
                qid = harness.add_question("scout", "background note")
                row = next(r for r in ctx.load_pending_questions() if r["id"] == qid)
                self.assertIsNone(row["correlation_id"])
            finally:
                ctx.close()
                scope.close()

    def test_explicit_job_id_does_not_borrow_current_correlation(self) -> None:
        # An explicit job_id points at a turn we did NOT resolve here; guessing
        # its correlation from `current` would write a self-inconsistent row
        # (job_id=X, correlation=Y). So correlation must stay None.
        with tempfile.TemporaryDirectory() as td:
            job = _FakeJob(job_id=7, correlation_id="op:9:9")
            ctx, scope, harness, _ = self._wire(td, job)
            try:
                qid = harness.add_question("scout", "q", job_id=3)
                row = next(r for r in ctx.load_pending_questions() if r["id"] == qid)
                self.assertEqual(row["job_id"], 3)
                self.assertIsNone(row["correlation_id"])
            finally:
                ctx.close()
                scope.close()


if __name__ == "__main__":
    unittest.main()
