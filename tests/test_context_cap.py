"""Unit tests for ``_context_cap_for`` — the per-agent context-window
estimate that feeds the token-usage gauge.

Regression: an agent with no explicit model (``effective_model`` → None,
i.e. running on the Claude Code default) used to report a 200k window,
but the current default is the 1M-context opus, so the gauge under-read.
"""

from __future__ import annotations

import unittest

from salient_core.daemon._prompts import _context_cap_for


class ContextCapTests(unittest.TestCase):
    def test_default_model_is_one_million(self):
        # No model + no override → the Claude Code default (1M opus).
        self.assertEqual(_context_cap_for(None, {}), 1_000_000)

    def test_explicit_cap_override_wins(self):
        self.assertEqual(_context_cap_for(None, {"context_cap": 500_000}), 500_000)
        self.assertEqual(_context_cap_for("claude-opus-4-8", {"context_cap": 123}), 123)

    def test_opus_ids_are_one_million(self):
        for m in (
            "claude-opus-4-8",
            "claude-opus-4-8[1m]",
            "opus",
            "claude-opus-4-7",
            "us.anthropic.claude-opus-4-8-v1:0",
        ):
            self.assertEqual(_context_cap_for(m, {}), 1_000_000, m)

    def test_one_m_marker_is_detected(self):
        # Any id carrying the 1M window marker → 1M (future-proofing).
        self.assertEqual(_context_cap_for("some-model[1m]", {}), 1_000_000)

    def test_smaller_models_are_two_hundred_k(self):
        for m in ("sonnet", "claude-sonnet-4-6", "haiku", "claude-haiku-4-5"):
            self.assertEqual(_context_cap_for(m, {}), 200_000, m)


if __name__ == "__main__":
    unittest.main()
