"""The curated public API surface: everything an app needs imports from the
top-level ``salient_core`` package (not private ``_*`` modules), and the two
downstream-facing convenience helpers behave.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import salient_core
from salient_core import (
    ActionLedger,
    AgentRunner,
    ContextStore,
    DaemonServices,
    KnowledgeGraph,
    QuestionInbox,
    bucketed_profile,
    make_bus,
    semantic_recall,
)

SUBJ = "learner:op"


class PublicSurfaceTests(unittest.TestCase):
    def test_all_names_importable_from_top_level(self):
        for name in salient_core.__all__:
            self.assertTrue(hasattr(salient_core, name), f"missing public name: {name}")

    def test_type_checking_block_mirrors_lazy_exports(self):
        # __all__ is derived from _LAZY_EXPORTS, so the only way the static
        # (mypy/py.typed) and runtime surfaces can drift is the TYPE_CHECKING
        # import block. Parse it and assert it names exactly the lazy exports.
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(salient_core))
        static_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
            for stmt in node.body
            if isinstance(stmt, ast.ImportFrom)
            for alias in stmt.names
        }
        self.assertEqual(static_names, set(salient_core._LAZY_EXPORTS))

    def test_key_primitives_are_the_canonical_objects(self):
        # Sanity: the top-level re-export is the same object as the subpackage's.
        self.assertIs(ContextStore, salient_core.bus.ContextStore)
        self.assertIs(KnowledgeGraph, salient_core.memory.KnowledgeGraph)
        self.assertIs(QuestionInbox, salient_core.coord.QuestionInbox)
        self.assertIs(salient_core.LocalClaudeBackend, salient_core.daemon.LocalClaudeBackend)
        self.assertIs(
            salient_core.codex_command_is_read_only,
            salient_core.codex.codex_command_is_read_only,
        )
        # Downstream approval handlers classify with the public function; the
        # mixin's private staticmethod must stay behavior-identical to it.
        self.assertTrue(salient_core.codex_command_is_read_only({"command": "git status --short"}))
        self.assertFalse(salient_core.codex_command_is_read_only({"command": "rm -rf /"}))
        # Referenced so the imports are load-bearing, not dead.
        self.assertTrue(callable(make_bus))
        self.assertTrue(callable(AgentRunner))
        self.assertTrue(callable(ActionLedger))
        self.assertTrue(hasattr(DaemonServices, "add_question"))  # Protocol method


class SemanticRecallTests(unittest.TestCase):
    def test_degrades_to_empty_without_embedder(self):
        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg.close)
            # profile=None → get_embedder returns None → no crash, empty result.
            out = asyncio.run(semantic_recall(kg, None, "how to memorize ports"))
            self.assertEqual(out, [])
            # Blank query short-circuits too.
            self.assertEqual(asyncio.run(semantic_recall(kg, {}, "   ")), [])

    def test_degrades_when_embedder_raises(self):
        # The docstring promises "it never raises" — an embedder whose HTTP
        # call blows up must degrade to [], not propagate.
        class _ExplodingEmbedder:
            model = "boom"

            async def embed_one(self, text):
                raise RuntimeError("endpoint down")

        from salient_core.memory import recall as recall_mod

        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg.close)
            with mock.patch.object(
                recall_mod, "get_embedder", lambda profile: _ExplodingEmbedder()
            ):
                out = asyncio.run(semantic_recall(kg, {"embeddings": {}}, "query"))
            self.assertEqual(out, [])


class BucketedProfileTests(unittest.TestCase):
    def test_buckets_and_recall(self):
        now = 1_000_000.0
        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg.close)
            kg.record_learner_review(
                SUBJ,
                "kerberoast",
                predicate="strong_topic",
                mastery=0.9,
                review_due=now + 6 * 86400,
                agent="tutor",
                now=now - 9 * 86400,
            )
            kg.record_learner_review(
                SUBJ,
                "ssrf",
                predicate="weak_topic",
                mastery=0.3,
                review_due=now - 3 * 86400,
                agent="tutor",
                now=now - 8 * 86400,
            )
            kg.assert_fact(
                SUBJ, "misconception", "ssrf is only external", agent="tutor", expires_at=None
            )

            prof = bucketed_profile(kg, SUBJ, now=now)

            self.assertEqual([e["topic"] for e in prof["due"]], ["ssrf"])  # overdue weak
            self.assertEqual(prof["strong"][0]["topic"], "kerberoast")
            self.assertEqual(prof["counts"]["misconceptions"], 1)
            self.assertEqual(prof["counts"]["strong"], 1)
            self.assertEqual(prof["counts"]["weak"], 1)
            # recall comes from the real forgetting curve and is in range.
            r = prof["strong"][0]["recall"]
            self.assertTrue(r is None or 0.0 <= r <= 1.0)


if __name__ == "__main__":
    unittest.main()
