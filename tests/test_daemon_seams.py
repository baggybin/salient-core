"""Daemon injection seams: tool-builder registry, prompt-root override, and
the inert verification_leg passthrough. Mirror the alias / policy seams so a
downstream skin plugs its factories + prompts into the kernel daemon.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from salient_core import daemon
from salient_core.daemon import _prompts, _tool_registry
from salient_core.daemon._helpers import Job


class ToolBuilderSeamTests(unittest.TestCase):
    def tearDown(self):
        _tool_registry.reset()

    def test_default_stub_raises_loudly(self):
        _tool_registry.reset()
        with self.assertRaises(NotImplementedError):
            daemon.get_tool_builder()("nmap", {})

    def test_default_subagent_builder_returns_empty(self):
        _tool_registry.reset()
        self.assertEqual(daemon.get_subagent_builder()("agent", [], {}), [])

    def test_set_tool_builder_swaps(self):
        daemon.set_tool_builder(
            lambda *a, **k: ("srv", "wire", ["t"]),
            lambda *a, **k: ["sub"],
        )
        self.assertEqual(daemon.get_tool_builder()("t", {}), ("srv", "wire", ["t"]))
        self.assertEqual(daemon.get_subagent_builder()("a", [], {}), ["sub"])

    def test_set_tool_builder_leaves_subagents_default_when_omitted(self):
        daemon.set_tool_builder(lambda *a, **k: ("srv", "wire", []))
        self.assertEqual(daemon.get_subagent_builder()("a", [], {}), [])


class PromptRootSeamTests(unittest.TestCase):
    def tearDown(self):
        _prompts.set_prompts_root(_prompts._DEFAULT_PROMPTS_ROOT)

    def test_default_root_loads_packaged_addenda(self):
        _prompts.set_prompts_root(_prompts._DEFAULT_PROMPTS_ROOT)
        self.assertTrue(_prompts._load_agent_protocol())

    def test_set_prompts_root_overrides_and_clears_cache(self):
        # prime the cache from the default, then override
        _prompts._load_agent_protocol()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "agent_protocol.md").write_text("CUSTOM ADDENDUM")
            _prompts.set_prompts_root(p)
            self.assertEqual(_prompts._load_agent_protocol(), "CUSTOM ADDENDUM")

    def test_missing_addendum_raises_pointed_error(self):
        with tempfile.TemporaryDirectory() as td:
            _prompts.set_prompts_root(Path(td))
            with self.assertRaises(FileNotFoundError):
                _prompts._load_recipe_discipline()


class VerificationLegPassthroughTests(unittest.TestCase):
    def test_job_defaults_false(self):
        j = Job(id=1, prompt="p", submitted_at=0.0)
        self.assertFalse(j.verification_leg)

    def test_job_round_trips_flag(self):
        j = Job(id=1, prompt="p", submitted_at=0.0, verification_leg=True)
        self.assertTrue(j.verification_leg)
        # inert: the field carries, the rest of the job is unchanged
        self.assertEqual((j.id, j.prompt, j.result, j.error), (1, "p", "", None))


if __name__ == "__main__":
    unittest.main()
