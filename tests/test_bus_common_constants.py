"""Pin the invariant: shared helpers in salient.bus._common must
resolve their private constants at call time.

Mirrors tests/test_tools_common_constants.py — when the tools/ monolith
split (commit 2d93ae7), several constants were stranded outside
_common.py and the helpers that referenced them bare-named NameError'd
at runtime on default code paths. This test exercises every shared
helper in salient/bus/_common.py on a realistic input so the same
class of bug would fail loudly here too.

See: feedback_monolith_split_constant_leak.md.
"""

from __future__ import annotations

import unittest

from salient_core.bus import _common


class CommonHelpersResolveConstantsTests(unittest.TestCase):
    def test_text_wraps_string_payload(self):
        out = _common._text("hello")
        self.assertEqual(out["content"][0]["text"], "hello")
        self.assertFalse(out.get("is_error"))

    def test_text_marks_errors(self):
        out = _common._text("boom", error=True)
        self.assertTrue(out["is_error"])

    def test_delegation_gated_handles_truthy_wildcard(self):
        # The gate accepts True, '*', or ['*'] as "always gate".
        self.assertTrue(_common._delegation_gated(True, "scanner"))
        self.assertTrue(_common._delegation_gated("*", "scanner"))
        self.assertTrue(_common._delegation_gated(["*"], "scanner"))
        # Empty / None / [] = no gating.
        self.assertFalse(_common._delegation_gated(None, "scanner"))
        self.assertFalse(_common._delegation_gated([], "scanner"))
        # Explicit member list gates only matching targets.
        self.assertTrue(_common._delegation_gated(["scanner"], "scanner"))
        self.assertFalse(_common._delegation_gated(["scanner"], "bash"))

    def test_compute_ask_agent_timeout_uses_per_turn_and_hard_cap(self):
        """References _PER_TURN_SECS and _TIMEOUT_HARD_CAP_SECS."""

        # Minimal stub runner with the attrs the helper reads.
        class _R:
            prompt_timeout = 60

        out = _common._compute_ask_agent_timeout(
            daemon=None,
            target_name="scanner",
            target_runner=_R(),
            max_turns_hint=5,
        )
        # 5 turns × _PER_TURN_SECS = 300, plus prompt_timeout slop, well
        # under the hard cap.
        self.assertIsNotNone(out)
        self.assertGreater(out, 0)
        self.assertLessEqual(out, _common._TIMEOUT_HARD_CAP_SECS)

    def test_parse_delegation_answer_recognizes_approve(self):
        """References _APPROVE_WORDS."""
        verdict, _ = _common._parse_delegation_answer("yes go ahead")
        self.assertEqual(verdict, "approve")

    def test_parse_delegation_answer_recognizes_deny(self):
        """References _DENY_WORDS."""
        verdict, _ = _common._parse_delegation_answer("no, do not")
        self.assertEqual(verdict, "deny")

    def test_extract_targets_from_text_uses_hostname_re(self):
        """References _HOSTNAME_RE and _IPV4_RE."""
        targets = _common._extract_targets_from_text("scan 10.0.0.1 and example.com please")
        self.assertIn("10.0.0.1", targets)

    def test_context_read_cap_returns_int(self):
        """References _DEFAULT_CONTEXT_READ_CAP."""
        cap = _common._context_read_cap()
        self.assertIsInstance(cap, int)
        self.assertGreater(cap, 0)

    def test_format_swarm_payload_returns_json(self):
        import json

        out = _common._format_swarm_payload(
            parent_call_id=42,
            aggregate="all",
            results=[{"member": "m1", "reply": "found x"}],
            warnings=[],
        )
        # Should be parseable JSON containing the parent_call_id.
        parsed = json.loads(out)
        self.assertEqual(parsed.get("parent_call_id"), 42)

    def test_redact_operator_infra_runs_without_error(self):
        """References regex constants. Smoke-call with a minimal
        daemon stub — the helper signature is (prompt, daemon) and
        returns (redacted_prompt, list_of_placeholders)."""

        class _D:
            profile = {"network": {"lhost": "10.99.0.42", "lport": 4444}}

        out, placeholders = _common._redact_operator_infra(
            "Setting LHOST=10.99.0.42 LPORT=4444",
            _D(),
        )
        self.assertIsInstance(out, str)
        self.assertIsInstance(placeholders, list)

    def test_redacts_secret_shaped_tokens(self):
        """API keys / PATs / secret env-assignments in delegation prose
        must be stripped before crossing the bus, and the operator log
        must NOT contain the secret value (it records leaks, can't be one)."""

        class _D:
            profile = {}

        secrets = {
            "sk-abcdefghijklmnopqrstuvwxyz123456": "<redacted-key>",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789": "<redacted-token>",
            "xoxb-12345-abcdefghij": "<redacted-token>",
            "AKIAIOSFODNN7EXAMPLE": "<redacted-key>",
        }
        for secret, placeholder in secrets.items():
            out, log = _common._redact_operator_infra(f"use {secret} now", _D())
            self.assertNotIn(secret, out, f"{secret!r} survived redaction")
            self.assertIn(placeholder, out)
            # The value must never appear in the redaction log.
            self.assertFalse(
                any(secret in entry for entry in log),
                f"secret value leaked into the redaction log: {log}",
            )

    def test_secret_env_assignment_redacted(self):
        class _D:
            profile = {}

        out, _ = _common._redact_operator_infra(
            "export DEEPSEEK_API_KEY=sk-deepseek-secretvalue123",
            _D(),
        )
        self.assertNotIn("secretvalue123", out)
        self.assertIn("DEEPSEEK_API_KEY=<redacted>", out)

    def test_no_false_positive_on_plain_targets(self):
        """A normal prompt with target IPs/ports + ordinary words must be
        untouched by the secret pass (no over-redaction)."""

        class _D:
            profile = {}

        prompt = "Scan 192.168.1.5 on port 8080 — enumerate the task-runner."
        out, log = _common._redact_operator_infra(prompt, _D())
        self.assertEqual(out, prompt)
        self.assertEqual(log, [])


class CommonAllListIsExhaustiveTests(unittest.TestCase):
    """Smoke check: every name claimed in _common.__all__ resolves at
    the bare-name level after `from ._common import *` — the
    per-group factories rely on this."""

    def test_every_all_name_resolves(self):
        names = list(getattr(_common, "__all__", []))
        self.assertGreater(len(names), 0, "_common.__all__ must not be empty")
        missing = [n for n in names if not hasattr(_common, n)]
        self.assertFalse(
            missing,
            f"_common.__all__ claims {missing} but the names aren't "
            f"actually defined in the module.",
        )


if __name__ == "__main__":
    unittest.main()
