"""The job_capture side-channel on the REAL delegation `ask_agent` tool.

`ask_consensus` scopes each panel leg's event-trace query to the child Job it
dispatched, and the only carrier for that id is a mutable `job_capture` dict.
Since ask_agent migrated onto @bus_tool(routed=True), that dict rides on the
typed flags channel (`BusFlags.job_capture`), NOT an `_job_capture` key in args —
model_dump on the args model would sever the write-back alias. Internal
producers (consensus legs, ask_partner, swarm children) dispatch through
`.trusted(args, *, flags)`, never the model-facing `.handler`.

The consensus tests mirror this write-back in their fake ask_agent — so a
regression THERE would pass every consensus test. This drives the production
tool end-to-end to pin the contract:

  * a `job_capture` dict on the flags receives the dispatched child Job id, and
  * that id never leaks into the model-visible reply payload.

Deleting the write-back in `_delegation.py` must fail this test.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
import unittest

from salient_core.bus import _delegation as D
from salient_core.bus._flags import BusFlags


class _Job:
    def __init__(self, job_id: int, result: str, verification_leg: bool = False) -> None:
        self.id = job_id
        self.result = result
        self.error = None
        self.verification_leg = verification_leg


class _WorkerRunner:
    """A target runner whose `submit` resolves the caller's future inline, so
    the handler reaches the write-back and returns without a real event loop of
    child work. `subscribe`/`_log_provenance` are intentionally absent — the
    handler wraps both in `suppress(Exception)`, so their absence exercises the
    no-echo degrade path (child_q stays None)."""

    def __init__(self, name: str, job_id: int) -> None:
        self.name = name
        self.status = "idle"
        self.prompt_timeout = 0
        self._job_id = job_id

    def submit(self, prompt, *, future, max_turns_hint=None, verification_leg=False):
        job = _Job(self._job_id, result=f"{self.name} reply", verification_leg=verification_leg)
        self.last_verification_leg = verification_leg  # for the carry-through test
        future.set_result(job)
        return job


class _CaptureDaemon:
    """Minimal daemon satisfying exactly the surface the happy-path dispatch of
    a bus_trusted caller to an already-running target touches."""

    def __init__(self, runner: _WorkerRunner) -> None:
        self.profile: dict = {}
        self.inbox = None
        self.all_cfgs = {"caller": {"bus_trusted": True}, runner.name: {}}
        self.runners = {runner.name: runner}
        self._bus_calls: dict = {}
        self._next_call = 1

    def bus_call_admission_check(self, caller):
        return None

    def bus_call_register(self, *a, **k):
        cid = self._next_call
        self._next_call += 1
        return cid

    def bus_call_set_future(self, call_id, future):
        pass

    def bus_call_set_state(self, call_id, state):
        pass

    def bus_call_set_child_job(self, call_id, job_id):
        pass

    def bus_call_resolve(self, call_id):
        pass

    def expand_prompt(self, text):
        return text, []

    # Redispatch gate: only reached on the WIRE path (default flags don't skip
    # it). Threshold high + count 0 ⇒ the gate passes without an operator Q, so
    # the wire happy-path reaches the runner and the write-back line.
    def _redispatch_threshold(self):
        return 100

    def _redispatch_check(self, owner, name):
        return 0

    def _redispatch_increment(self, owner, name):
        pass


def _ask_agent(daemon):
    # ask_agent is the first tool in make_delegation_tools.
    return D.make_delegation_tools(daemon, "caller")[0]


class JobCaptureSideChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # `salient_core.engagement` is a downstream-provided module absent from
        # the kernel package; the handler imports `is_agent_disabled` from it at
        # runtime. Stub it (returns False — nothing disabled) so the REAL
        # ask_agent body executes end-to-end here.
        mod = types.ModuleType("salient_core.engagement")
        mod.is_agent_disabled = lambda profile, name: False
        sys.modules["salient_core.engagement"] = mod
        self.addCleanup(lambda: sys.modules.pop("salient_core.engagement", None))

    async def test_capture_dict_receives_child_job_id(self):
        runner = _WorkerRunner("worker", job_id=4242)
        daemon = _CaptureDaemon(runner)
        cap: dict = {}
        result = await asyncio.wait_for(
            _ask_agent(daemon).trusted(
                {"name": "worker", "prompt": "summarize the notes"},
                flags=BusFlags(skip_redispatch_gate=True, job_capture=cap),
            ),
            timeout=2.0,
        )
        # The real handler wrote the dispatched job id back into the dict.
        self.assertEqual(cap.get("job_id"), 4242)
        # ...and it stayed off the model-visible reply.
        text = result["content"][0]["text"]
        self.assertNotIn("job_id", text)
        self.assertNotIn("4242", text)
        self.assertFalse(result.get("is_error"))

    def test_trusted_signature_exposes_flags_kwarg(self):
        # The internal producers (consensus legs, ask_partner, swarm children)
        # dispatch via `ask_agent.trusted(args, flags=BusFlags(...))`. This pins
        # that the routed `.trusted` entry really exposes a keyword-only `flags`
        # param — if a refactor dropped it, those producers would break at
        # runtime; this goes red first.
        trusted = _ask_agent(_CaptureDaemon(_WorkerRunner("worker", job_id=1))).trusted
        params = inspect.signature(trusted).parameters
        self.assertIn("flags", params)
        self.assertEqual(params["flags"].kind, inspect.Parameter.KEYWORD_ONLY)

    async def test_typed_flags_take_effect_on_real_handler(self):
        # Drive the REAL routed body via the exact producer contract: pass the
        # flag as a typed BusFlags on .trusted. _CaptureDaemon deliberately
        # implements no `_redispatch_*` methods, so if skip_redispatch_gate did
        # NOT take effect the handler would hit daemon._redispatch_threshold()
        # and AttributeError — success proves the flag reached the body AND the
        # gate was genuinely skipped.
        runner = _WorkerRunner("worker", job_id=99)
        daemon = _CaptureDaemon(runner)
        result = await asyncio.wait_for(
            _ask_agent(daemon).trusted(
                {"name": "worker", "prompt": "summarize the notes"},
                flags=BusFlags(skip_redispatch_gate=True),
            ),
            timeout=2.0,
        )
        self.assertFalse(result.get("is_error"))
        self.assertIn("worker reply", result["content"][0]["text"])

    async def test_no_capture_dict_is_harmless(self):
        runner = _WorkerRunner("worker", job_id=7)
        daemon = _CaptureDaemon(runner)
        # No job_capture on the flags — dispatch must still succeed.
        result = await asyncio.wait_for(
            _ask_agent(daemon).trusted(
                {"name": "worker", "prompt": "summarize the notes"},
                flags=BusFlags(skip_redispatch_gate=True),
            ),
            timeout=2.0,
        )
        self.assertFalse(result.get("is_error"))
        self.assertIn("worker reply", result["content"][0]["text"])

    async def test_wire_path_cannot_smuggle_job_capture(self):
        # A model on the wire `.handler` path passes an `_job_capture` key: it is
        # a declared-args violation the bus_tool ingress DROPS. Dispatch still
        # succeeds (extras are dropped, not 400'd) and the wire caller gets
        # default flags (job_capture is None), so the write-back is a no-op —
        # the model cannot make ask_agent write a job id into a dict it supplied.
        runner = _WorkerRunner("worker", job_id=555)
        daemon = _CaptureDaemon(runner)
        cap: dict = {}
        result = await asyncio.wait_for(
            _ask_agent(daemon).handler(
                {"name": "worker", "prompt": "go", "_job_capture": cap},
            ),
            timeout=2.0,
        )
        self.assertFalse(result.get("is_error"))
        self.assertEqual(cap, {})  # nothing written back into the model's dict
        self.assertNotIn("555", result["content"][0]["text"])

    async def test_verification_leg_flag_carries_through_to_the_job(self):
        # MECHANISM ONLY: the bus forwards flags.verification_leg into
        # runner.submit(verification_leg=), which stamps the child Job. The kernel
        # never READS it — a downstream (skin) verifier does. This pins the
        # carry-channel that completes core's inert verification seam; if a future
        # change drops the thread, the seam goes dangling and this goes red.
        for leg in (True, False):
            runner = _WorkerRunner("worker", job_id=1)
            daemon = _CaptureDaemon(runner)
            await asyncio.wait_for(
                _ask_agent(daemon).trusted(
                    {"name": "worker", "prompt": "recheck the fact"},
                    flags=BusFlags(skip_redispatch_gate=True, verification_leg=leg),
                ),
                timeout=2.0,
            )
            self.assertIs(runner.last_verification_leg, leg)


if __name__ == "__main__":
    unittest.main()
