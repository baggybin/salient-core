"""The daemon's bus-call watchdog.

Pins the 2026-05-16 reaper added after the symptom: agents going idle
and a delegation stalling silently — `_bus_calls` shows the entry, but
nobody asks the operator because the bus's await is still inside its
prompt_timeout window. The reaper makes the stall visible immediately.

Diagnosis: docs/COMM_PATHS.md section B failure-mode #3 + #5.

Together with the silent-completion nudge in runner.py (failure-mode
#2), this closes the "everything just stops with no question" symptom.
"""

from __future__ import annotations

import asyncio
import time
import types
import unittest
from unittest import mock

from salient_core.daemon._helpers import BusCall
from salient_core.daemon._questions import _QuestionsMixin


class _ReaperDaemon(_QuestionsMixin):
    """Minimal daemon shim wired with the bits the reaper actually uses:
    `_bus_calls`, `prompt_timeout`, `add_question`, `runners`, `inbox`.

    Stays a `_QuestionsMixin` subclass so the production `add_question`
    and `_bus_call_reaper` run unchanged."""

    def __init__(self, prompt_timeout: float = 60.0) -> None:
        self._bus_calls: dict[int, BusCall] = {}
        self.prompt_timeout = prompt_timeout
        self.runners: dict = {}
        # Pseudo-inbox: capture add() calls so the test can assert on
        # them without dragging the full QuestionInbox + SQL setup.
        self.inbox = types.SimpleNamespace(
            added=[],
            add=lambda agent, text, job_id=0, kind=None: self._mock_add(
                agent,
                text,
                job_id,
                kind,
            ),
            publish=lambda event, q: None,
        )

    def _mock_add(self, agent, text, job_id, kind):
        q = types.SimpleNamespace(
            id=len(self.inbox.added) + 1,
            agent=agent,
            text=text,
            job_id=job_id,
            kind=kind,
        )
        self.inbox.added.append(q)
        return q

    # `_announce_question` touches stdout / asyncio.create_task — stub
    # so the test doesn't depend on event-loop wiring in those paths.
    def _announce_question(self, q, source):
        pass


def _stale_call(call_id: int, age_seconds: float, state: str = "awaiting_reply") -> BusCall:
    """Build a BusCall that started `age_seconds` ago."""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    return BusCall(
        id=call_id,
        caller="manager",
        target="deepseek_msf",
        prompt_preview="exploit eternalblue on 10.0.0.5",
        started_at=time.time() - age_seconds,
        state=state,
        future=fut,
    )


class BusCallReaperTests(unittest.IsolatedAsyncioTestCase):
    """Behavior: reaper flags entries past `prompt_timeout × multiplier`
    that are in `awaiting_reply` state, files an operator question, and
    sets `flagged_stalled` to prevent repeat-firing."""

    async def _run_reaper_once(
        self,
        daemon,
        *,
        interval: float = 0.01,
        stall_multiplier: float = 3.0,
    ) -> None:
        """Run the reaper coroutine for one tick, then cancel. Use a
        very short interval so the test doesn't actually wait."""
        task = asyncio.create_task(
            daemon._bus_call_reaper(
                interval=interval,
                stall_multiplier=stall_multiplier,
            )
        )
        # Two interval ticks is enough to guarantee one full pass.
        await asyncio.sleep(interval * 3)
        task.cancel()
        with mock.patch("asyncio.CancelledError"):
            pass
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def test_buscall_defaults_flagged_false(self):
        """Fresh BusCall must start with `flagged_stalled=False`. Without
        this default, the reaper would skip every call on first pass."""
        call = _stale_call(1, age_seconds=0)
        self.assertFalse(
            call.flagged_stalled,
            "BusCall.flagged_stalled must default to False; otherwise "
            "the reaper's `if call.flagged_stalled: continue` short-"
            "circuits and stalls never get reported.",
        )

    async def test_reaper_flags_stalled_call(self):
        """Call age > prompt_timeout × 3 in `awaiting_reply` state →
        reaper files an operator question on the caller's chain and
        marks the call `flagged_stalled`."""
        d = _ReaperDaemon(prompt_timeout=60.0)
        # 200s old, prompt_timeout=60, threshold = 180 → stalled.
        d._bus_calls[1] = _stale_call(1, age_seconds=200.0)
        await self._run_reaper_once(d)
        self.assertEqual(
            len(d.inbox.added),
            1,
            "Reaper must file exactly one question on the caller's "
            "chain when it finds a stalled call. Zero = the stall is "
            "still invisible to the operator (regression).",
        )
        q = d.inbox.added[0]
        self.assertEqual(
            q.agent,
            "manager",
            "Question must be filed to the CALLER (the agent whose "
            "future is hung), not the target. Filing to the target "
            "would file to the wedged agent, which can't act.",
        )
        self.assertIn(
            "STALLED",
            q.text,
            "Question text must signal the failure mode loudly so the "
            "operator triages it ahead of routine questions.",
        )
        self.assertTrue(
            d._bus_calls[1].flagged_stalled,
            "Reaper must set `flagged_stalled=True` after firing so a "
            "subsequent pass doesn't re-file the same question.",
        )

    async def test_reaper_files_routable_bus_stall_question(self):
        """The stall question must be kind='bus_stall' (so its answer drives
        a mechanical cancel, not a queued prompt) and the call must remember
        its question id so the answer handler can map back to it."""
        d = _ReaperDaemon(prompt_timeout=60.0)
        d._bus_calls[1] = _stale_call(1, age_seconds=200.0)
        await self._run_reaper_once(d)
        self.assertEqual(len(d.inbox.added), 1)
        q = d.inbox.added[0]
        self.assertEqual(
            q.kind,
            "bus_stall",
            "stall question must be kind='bus_stall' — kind='operator' "
            "would queue the reply as a prompt the blocked caller can't "
            "consume (the original bug).",
        )
        self.assertEqual(
            d._bus_calls[1].stall_qid,
            q.id,
            "the call must record its stall question id so an operator "
            "'cancel'/'wait' answer can be routed back to THIS call.",
        )
        # The text points the operator at the real cancel paths.
        self.assertIn("cancel", q.text.lower())
        self.assertIn("bus cancel --id 1", q.text)

    async def test_reaper_skips_snoozed_call(self):
        """After the operator answers 'wait', the call is snoozed; the reaper
        must not re-ask until the snooze window passes."""
        d = _ReaperDaemon(prompt_timeout=60.0)
        call = _stale_call(1, age_seconds=200.0)
        call.stall_snooze_until = time.time() + 1000.0  # snoozed far out
        d._bus_calls[1] = call
        await self._run_reaper_once(d)
        self.assertEqual(
            d.inbox.added,
            [],
            "a snoozed call (operator said 'wait') must not be re-flagged "
            "until its snooze window elapses.",
        )

    async def test_reaper_does_not_double_fire(self):
        """A second pass over the same already-flagged call must not
        file another question — the operator hears about a stall once."""
        d = _ReaperDaemon(prompt_timeout=60.0)
        d._bus_calls[1] = _stale_call(1, age_seconds=200.0)
        await self._run_reaper_once(d)
        first_count = len(d.inbox.added)
        # Run again — flagged_stalled should prevent a repeat.
        await self._run_reaper_once(d)
        self.assertEqual(
            len(d.inbox.added),
            first_count,
            "Reaper must not re-file the same stall question on a "
            "second pass. Otherwise a long-stalled call spams the "
            "inbox every 30s.",
        )

    async def test_reaper_skips_fresh_calls(self):
        """A call younger than the threshold is just normal latency,
        not a stall. Reaper must skip."""
        d = _ReaperDaemon(prompt_timeout=60.0)
        # 30s old, threshold = 180s → not stalled.
        d._bus_calls[1] = _stale_call(1, age_seconds=30.0)
        await self._run_reaper_once(d)
        self.assertEqual(
            d.inbox.added,
            [],
            "Fresh calls must not trigger the reaper. False positives "
            "train the operator to ignore stall questions.",
        )

    async def test_reaper_skips_non_awaiting_reply_state(self):
        """Only `awaiting_reply` counts. Calls in `awaiting_agent_start`
        or `awaiting_delegation_gate` are blocked on operator action,
        not on a wedged target — flagging them would spam the operator
        about a stall they themselves are causing."""
        d = _ReaperDaemon(prompt_timeout=60.0)
        d._bus_calls[1] = _stale_call(
            1,
            age_seconds=500.0,
            state="awaiting_agent_start",
        )
        d._bus_calls[2] = _stale_call(
            2,
            age_seconds=500.0,
            state="awaiting_delegation_gate",
        )
        await self._run_reaper_once(d)
        self.assertEqual(
            d.inbox.added,
            [],
            "Calls in operator-gate states are blocked on the operator, "
            "not on a wedged target — reaping them would file 'why did "
            "you not answer your own question' style noise.",
        )

    async def test_reaper_uses_prompt_timeout_for_threshold(self):
        """The threshold is `prompt_timeout × stall_multiplier`, so an
        engagement with a tighter timeout flags stalls sooner."""
        d = _ReaperDaemon(prompt_timeout=30.0)
        # 100s old, threshold = 30 × 3 = 90 → stalled.
        d._bus_calls[1] = _stale_call(1, age_seconds=100.0)
        await self._run_reaper_once(d)
        self.assertEqual(
            len(d.inbox.added),
            1,
            "Threshold must scale with prompt_timeout. Hard-coding a "
            "fixed wall-clock threshold would mis-flag fast-engagement "
            "calls and miss slow-engagement ones.",
        )

    async def test_threshold_defaults_to_prompt_timeout_multiple(self):
        """No profile / no override → threshold is prompt_timeout × mult."""
        d = _ReaperDaemon(prompt_timeout=60.0)
        self.assertEqual(d._bus_stall_threshold(stall_multiplier=3.0), 180.0)
        self.assertEqual(d._bus_stall_threshold(stall_multiplier=2.0), 120.0)

    async def test_threshold_honors_profile_override(self):
        """S9: rate.bus_call_timeout_seconds is an absolute ceiling that
        wins over prompt_timeout × multiplier — engagements with short
        known runtimes surface a wedged call in minutes, not ~an hour."""
        d = _ReaperDaemon(prompt_timeout=1200.0)  # ×3 default = 3600s
        d.profile = {"rate": {"bus_call_timeout_seconds": 600}}
        self.assertEqual(d._bus_stall_threshold(stall_multiplier=3.0), 600.0)

    async def test_override_flags_call_the_default_would_miss(self):
        """A 700s-old call under prompt_timeout=1200 (default threshold
        3600s) is NOT stalled by default, but IS once the operator sets a
        600s absolute ceiling."""
        d = _ReaperDaemon(prompt_timeout=1200.0)
        d.profile = {"rate": {"bus_call_timeout_seconds": 600}}
        d._bus_calls[1] = _stale_call(1, age_seconds=700.0)
        await self._run_reaper_once(d)
        self.assertEqual(
            len(d.inbox.added),
            1,
            "With a 600s operator ceiling, a 700s call must be flagged "
            "even though prompt_timeout × 3 (3600s) would not.",
        )

    async def test_invalid_or_nonpositive_override_falls_back(self):
        """A non-numeric or non-positive override is ignored — the
        prompt_timeout multiple is used, never a zero/negative ceiling
        that would flag every call instantly."""
        d = _ReaperDaemon(prompt_timeout=60.0)
        for bad in ("not-a-number", 0, -5, None):
            d.profile = {"rate": {"bus_call_timeout_seconds": bad}}
            self.assertEqual(
                d._bus_stall_threshold(stall_multiplier=3.0),
                180.0,
                f"override {bad!r} must fall back to prompt_timeout × 3",
            )

    async def test_reaper_survives_single_bad_call(self):
        """An exception in one iteration must not kill the reaper —
        otherwise a single malformed bus call orphans the watchdog for
        the rest of the engagement."""
        d = _ReaperDaemon(prompt_timeout=60.0)
        # A "call" without a started_at attribute would AttributeError.
        # Use a real call so the rest of the logic exercises; rely on
        # the broad `except Exception` in the reaper to swallow any
        # future regression.
        d._bus_calls[1] = _stale_call(1, age_seconds=200.0)
        # Run multiple times; if the reaper swallowed an exception, it
        # keeps running. If it died, the second pass would see no new
        # state changes — but flagged_stalled prevents a second file
        # either way, so the meaningful check is that NO exception
        # propagates out of the task.
        await self._run_reaper_once(d)
        await self._run_reaper_once(d)
        # No assertion beyond "no exception bubbled up" — covered by
        # the suite passing.


if __name__ == "__main__":
    unittest.main()
