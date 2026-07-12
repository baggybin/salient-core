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
from salient_core.protocols import ToolBuildContext
from salient_core.runtime import ToolBundle


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

    def test_default_tool_bundle_builder_raises_loudly(self):
        _tool_registry.reset()
        with self.assertRaisesRegex(NotImplementedError, "tool bundle builder"):
            daemon.get_tool_bundle_builder()(
                "nmap",
                {},
                context=ToolBuildContext(None, None, "test"),
            )

    def test_tool_bundle_builder_coexists_with_legacy_builder_and_resets(self):
        bundle = ToolBundle()
        daemon.set_tool_builder(lambda *a, **k: ("srv", "wire", ["legacy"]))
        daemon.set_tool_bundle_builder(lambda *a, **k: bundle)

        self.assertIs(
            daemon.get_tool_bundle_builder()(
                "nmap",
                {},
                context=ToolBuildContext(None, None, "test"),
            ),
            bundle,
        )
        self.assertEqual(
            daemon.get_tool_builder()("nmap", {}),
            ("srv", "wire", ["legacy"]),
        )

        _tool_registry.reset()
        with self.assertRaises(NotImplementedError):
            daemon.get_tool_bundle_builder()(
                "nmap",
                {},
                context=ToolBuildContext(None, None, "test"),
            )


class KgBuilderSeamTests(unittest.TestCase):
    """set_kg_builder — same registry idiom, but the unregistered default
    BUILDS (the local SQLite KnowledgeGraph) rather than raises: the kernel
    has a perfectly good store of its own, the seam only lets a downstream
    swap in e.g. a network client with the same method surface."""

    def tearDown(self):
        _tool_registry.reset()

    def test_default_builds_a_local_knowledge_graph(self):
        from salient_core.memory.kg import KnowledgeGraph

        with tempfile.TemporaryDirectory() as td:
            kg = daemon.get_kg_builder()(Path(td) / "kg.db")
            try:
                self.assertIsInstance(kg, KnowledgeGraph)
                fact = kg.assert_fact("host:a", "related_to", "host:b")
                self.assertEqual([f.id for f in kg.query("host:a", None, None)], [fact.id])
            finally:
                kg.close()

    def test_default_accepts_a_string_path(self):
        # Downstreams pass whatever their --kg-db flag parsed to; the default
        # builder normalizes to Path rather than requiring one.
        with tempfile.TemporaryDirectory() as td:
            kg = daemon.get_kg_builder()(str(Path(td) / "kg.db"))
            try:
                self.assertTrue(kg.db_path.exists())
            finally:
                kg.close()

    def test_set_kg_builder_swaps_and_reset_restores(self):
        sentinel = object()
        daemon.set_kg_builder(lambda db_path: sentinel)
        self.assertIs(daemon.get_kg_builder()("ignored"), sentinel)
        _tool_registry.reset()
        self.assertIs(daemon.get_kg_builder(), _tool_registry._default_build_kg)


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
