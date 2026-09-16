"""Pre-write ``kg_assert`` validator seam.

A downstream skin may register a validator that REFUSES a kg_assert write
*before* the fact lands — unlike ``set_kg_assert_hook``, which fires post-write
and fail-open (a hook error is swallowed and the fact is already durable). The
kernel holds NO policy: it calls the validator on EVERY write path with the raw
triple and lets a raise refuse the write.

Motivation (salient-assay's receipts floor, 2026-09-16 handover): a seat could
hand-assert a bare ``verdict`` triple through the bus and it would land durably;
nothing in the loop could express "this claim has not been reproduced". The seam
lets a study skin refuse such a write at the wire.

Pins the handover's matrix:

* **deny** — a refused write raises the typed error and NO fact is written;
* **benign** — an allowed write lands exactly as before;
* **opt-out** — with no validator, behaviour is byte-identical to today;
* **both paths** — the same refusal is observed through the bus/MCP tool AND the
  in-process runner path (a validator guarding only one leaves the other open —
  "a gate only governs what routes through it");
* **pre-write ordering** — the fact is absent *inside* the validator's own call
  (proves ordering, not just the error text);
* **fail-closed** — a validator that itself raises (a skin bug) DENIES the write,
  never allows it silently ("infra failure never collapses into allowed").
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from salient_core.bus import (
    ClaimRefused,
    run_kg_assert_validator,
    set_kg_assert_validator,
)
from salient_core.bus._kg import make_kg_tools
from salient_core.daemon.runner import AgentRunner
from salient_core.memory.kg import KnowledgeGraph


def _text_of(reply: dict) -> str:
    for block in reply.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    return ""


class SeamUnitTests(unittest.TestCase):
    """The chokepoint helper both write paths cross, in isolation."""

    def tearDown(self):
        set_kg_assert_validator(None)  # module global — never leak into other tests

    def test_no_validator_returns_none(self):
        self.assertIsNone(run_kg_assert_validator("s", "p", "o", 1.0, "a", None))

    def test_allow_returns_none(self):
        set_kg_assert_validator(lambda *a: None)
        self.assertIsNone(run_kg_assert_validator("s", "p", "o", 1.0, "a", None))

    def test_claim_refused_surfaces_reason(self):
        def v(subject, predicate, obj, confidence, agent, contradicts):
            raise ClaimRefused("needs a case:/run: receipt — got prose")

        set_kg_assert_validator(v)
        reason = run_kg_assert_validator("study:x:1", "verdict", "prose", 1.0, "a", None)
        assert reason is not None
        self.assertIn("claim refused", reason)
        self.assertIn("receipt", reason)
        self.assertNotIn("validator error", reason)  # a policy refusal, not a bug

    def test_fail_closed_on_validator_bug(self):
        def v(*a):
            raise RuntimeError("boom")  # a bug, NOT a ClaimRefused

        set_kg_assert_validator(v)
        reason = run_kg_assert_validator("s", "p", "o", 1.0, "a", None)
        self.assertIsNotNone(reason)  # DENY, not allow
        assert reason is not None
        self.assertIn("validator error", reason)  # names the layer
        self.assertIn("RuntimeError", reason)

    def test_receives_exact_six_positional_args(self):
        # The handover fixes the signature (subject, predicate, object,
        # confidence, agent, contradicts); assay codes to it. A drift here would
        # TypeError their validator → fail-closed → deny every study write.
        seen: dict = {}

        def v(subject, predicate, obj, confidence, agent, contradicts):
            seen.update(
                subject=subject,
                predicate=predicate,
                obj=obj,
                confidence=confidence,
                agent=agent,
                contradicts=contradicts,
            )

        set_kg_assert_validator(v)
        run_kg_assert_validator("study:x:1", "verdict", "case:7", 0.9, "seat3", "other:1")
        self.assertEqual(
            seen,
            {
                "subject": "study:x:1",
                "predicate": "verdict",
                "obj": "case:7",
                "confidence": 0.9,
                "agent": "seat3",
                "contradicts": "other:1",
            },
        )


class WritePathTests(unittest.TestCase):
    """Both real write paths (bus/MCP tool + in-process runner) against a real KG."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = KnowledgeGraph(Path(self._td.name) / "kg.db")
        self.addCleanup(self.kg.close)
        self.daemon = SimpleNamespace(kg=self.kg, engagement_path=None, context=None)
        self.addCleanup(set_kg_assert_validator, None)

    # --- path drivers ---------------------------------------------------
    def _bus_assert(self, s, p, o, **kw):
        tool = make_kg_tools(self.daemon, "tester")[0]  # kg_assert is first
        args = {"subject": s, "predicate": p, "object": o, "confidence": 1.0, **kw}
        return asyncio.run(tool.trusted(args))

    def _runner_assert(self, s, p, o, **kw):
        me = SimpleNamespace(name="tester")  # _synthetic_dispatch only reads .name
        args = {"subject": s, "predicate": p, "object": o, "confidence": 1.0, **kw}
        return AgentRunner._synthetic_dispatch(me, self.daemon, "kg_assert", args)

    def _drive(self, which: str, s, p, o, **kw) -> tuple[bool, str]:
        """Return (ok, model_text) for either path, normalized."""
        if which == "bus":
            reply = self._bus_assert(s, p, o, **kw)
            err = bool(reply.get("isError") or reply.get("is_error"))
            return (not err), _text_of(reply)
        ok, text, _log = self._runner_assert(s, p, o, **kw)
        return bool(ok), text

    # --- matrix ---------------------------------------------------------
    def test_deny_writes_no_fact_both_paths(self):
        def v(subject, predicate, obj, confidence, agent, contradicts):
            raise ClaimRefused("needs a case:/run: receipt — got prose")

        set_kg_assert_validator(v)
        for which in ("bus", "runner"):
            with self.subTest(path=which):
                s = f"study:deny:{which}"
                ok, text = self._drive(which, s, "verdict", "prose")
                self.assertFalse(ok)
                self.assertIn("claim refused", text)
                self.assertIn("receipt", text)
                self.assertIsNone(
                    self.kg.get_exact(s, "verdict", "prose"),
                    "a refused write must not land",
                )

    def test_benign_allow_writes_both_paths(self):
        set_kg_assert_validator(lambda *a: None)
        for which in ("bus", "runner"):
            with self.subTest(path=which):
                s = f"study:ok:{which}"
                ok, text = self._drive(which, s, "verdict", "case:1")
                self.assertTrue(ok, text)
                self.assertIsNotNone(self.kg.get_exact(s, "verdict", "case:1"))

    def test_opt_out_writes_both_paths(self):
        # No validator registered ⇒ today's behaviour, unchanged.
        for which in ("bus", "runner"):
            with self.subTest(path=which):
                s = f"host:{which}"
                ok, text = self._drive(which, s, "runs", "svc:http")
                self.assertTrue(ok, text)
                self.assertIsNotNone(self.kg.get_exact(s, "runs", "svc:http"))

    def test_opt_out_bus_message_unchanged(self):
        # The success line is byte-identical to the pre-seam behaviour.
        reply = self._bus_assert("host:m", "runs", "svc:ssh", permanent=True)
        self.assertIn("recorded (permanent)", _text_of(reply))

    def test_fail_closed_denies_both_paths(self):
        def boom(*a):
            raise RuntimeError("skin bug")

        set_kg_assert_validator(boom)
        for which in ("bus", "runner"):
            with self.subTest(path=which):
                s = f"study:fc:{which}"
                ok, text = self._drive(which, s, "verdict", "case:1")
                self.assertFalse(ok, "a validator bug must DENY, not allow")
                self.assertIn("validator error", text)
                self.assertIsNone(self.kg.get_exact(s, "verdict", "case:1"))

    def test_pre_write_ordering_fact_absent_inside_validator(self):
        # Capture the observation in a closure dict so it survives the refusal
        # raise — an assertion *inside* the validator would be swallowed by the
        # fail-closed catch and never fail the test.
        observed: dict = {}

        def v(subject, predicate, obj, confidence, agent, contradicts):
            observed["present_at_call"] = self.kg.get_exact(subject, predicate, obj) is not None
            raise ClaimRefused("stop")

        set_kg_assert_validator(v)
        for which in ("bus", "runner"):
            with self.subTest(path=which):
                observed.clear()
                self._drive(which, f"study:ord:{which}", "verdict", "case:1")
                self.assertIn("present_at_call", observed, "validator was not called")
                self.assertFalse(
                    observed["present_at_call"],
                    "validator ran AFTER the write — the seam must be pre-write",
                )


if __name__ == "__main__":
    unittest.main()
