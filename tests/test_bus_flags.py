"""Typed bus routing flags (BusFlags): defaults, frozen/extra=forbid, and the
in-process-only side channels (on_event, job_capture). Wire ingress lives in
bus_tool now — a model can never set a routing flag — so there is no longer an
args→flags adapter to test here.
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from salient_core.bus import BusFlags


class BusFlagsTypeTests(unittest.TestCase):
    def test_defaults(self):
        f = BusFlags()
        self.assertFalse(f.skip_redispatch_gate)
        self.assertIsNone(f.parent_call_id)
        self.assertIsNone(f.on_event)

    def test_frozen(self):
        f = BusFlags(skip_redispatch_gate=True)
        with self.assertRaises(ValidationError):
            f.skip_redispatch_gate = False  # type: ignore[misc]

    def test_unknown_field_forbidden(self):
        with self.assertRaises(ValidationError):
            BusFlags(nope=True)  # type: ignore[call-arg]

    def test_on_event_excluded_from_dump_and_repr(self):
        f = BusFlags(on_event=lambda *a: None)
        self.assertNotIn("on_event", f.model_dump())
        self.assertNotIn("on_event", repr(f))

    def test_on_event_carried_in_process(self):
        sentinel = lambda *a: "hi"  # noqa: E731
        f = BusFlags(on_event=sentinel)
        self.assertIs(f.on_event, sentinel)

    def test_job_capture_preserves_dict_identity(self):
        # LOAD-BEARING: the whole point of job_capture is write-back by
        # reference. A plain `dict | None` annotation makes pydantic-core rebuild
        # the dict on construction (severing the alias); SkipValidation must keep
        # the SAME object so `flags.job_capture["job_id"] = ...` reaches the
        # producer's dict. If a future annotation "cleanup" drops SkipValidation,
        # this goes red instead of the write-back silently vanishing.
        cap: dict = {}
        f = BusFlags(job_capture=cap)
        self.assertIs(f.job_capture, cap)
        f.job_capture["job_id"] = 42
        self.assertEqual(cap["job_id"], 42)  # producer sees the handler's write

    def test_job_capture_excluded_from_dump_and_repr(self):
        f = BusFlags(job_capture={})
        self.assertNotIn("job_capture", f.model_dump())
        self.assertNotIn("job_capture", repr(f))

    def test_job_capture_defaults_none(self):
        self.assertIsNone(BusFlags().job_capture)


if __name__ == "__main__":
    unittest.main()
