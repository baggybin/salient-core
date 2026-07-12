"""KnowledgeGraph subject_prefix scoping on query / neighbors / embedding_counts.

A downstream namespace read-fence used to over-fetch a saturating
window then post-filter in Python; these tests pin the kernel-tier prefix
filter that makes fenced reads EXACT and bounded by the namespace size rather
than the graph's. Backward-compatible (None/"" prefix == today's behavior).
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from salient_core.memory.embeddings import pack_vector
from salient_core.memory.kg import KnowledgeGraph


def _kg(tmp: str) -> KnowledgeGraph:
    return KnowledgeGraph(Path(tmp) / "kg.db")


class SubjectPrefixQueryTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(self._td.name)
        self.addCleanup(self.kg.close)
        self.kg.assert_fact("host:secret", "has_service", "service:vault")
        self.kg.assert_fact("study:ps5:notes", "concerns", "host:secret")
        self.kg.assert_fact("study:ps5:notes", "covers", "heap grooming")
        self.kg.assert_fact("learner:op", "studies", "graphs")

    def test_prefix_returns_only_in_prefix_subjects(self):
        facts = self.kg.query(subject_prefix="study:")
        self.assertEqual({f.subject for f in facts}, {"study:ps5:notes"})
        self.assertEqual(len(facts), 2)  # the two study:ps5:notes facts

    def test_prefix_combines_with_substring_filter(self):
        facts = self.kg.query(object_="secret", subject_prefix="study:")
        self.assertEqual({f.subject for f in facts}, {"study:ps5:notes"})
        self.assertTrue(all("secret" in f.object for f in facts))

    def test_no_prefix_is_unrestricted(self):
        facts = self.kg.query(limit=50)
        self.assertEqual(
            {f.subject for f in facts},
            {"host:secret", "study:ps5:notes", "learner:op"},
        )

    def test_empty_prefix_is_unrestricted(self):
        facts = self.kg.query(subject_prefix="", limit=50)
        self.assertEqual(len(facts), 4)

    def test_prefix_respects_limit_as_newest_in_prefix(self):
        for i in range(5):
            self.kg.assert_fact(f"study:extra:{i}", "p", "o")
        facts = self.kg.query(subject_prefix="study:extra:", limit=2)
        self.assertEqual(len(facts), 2)
        # newest first (last inserted have the highest ts)
        self.assertEqual({f.subject for f in facts}, {"study:extra:4", "study:extra:3"})

    def test_prefix_like_wildcard_is_escaped(self):
        # An underscore in the prefix is LITERAL, not a single-char wildcard.
        self.kg.assert_fact("study_x", "p", "o")
        facts = self.kg.query(subject_prefix="study_")
        self.assertEqual({f.subject for f in facts}, {"study_x"})
        # study:ps5:notes (colon after "study") must NOT match if _ is escaped
        self.assertNotIn("study:ps5:notes", {f.subject for f in facts})


class SubjectPrefixNeighborsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(self._td.name)
        self.addCleanup(self.kg.close)
        # host:secret is a foreign hub touched by in-prefix facts + its own.
        self.kg.assert_fact("host:secret", "has_service", "service:vault")
        self.kg.assert_fact("host:secret", "located_in", "region:eu")
        self.kg.assert_fact("study:ps5:notes", "concerns", "host:secret")
        self.kg.assert_fact("study:ps5:notes", "covers", "heap grooming")
        self.kg.assert_fact("study:ps5:media", "streams", "host:secret")

    def test_foreign_seed_keeps_only_in_prefix_facts_pointing_at_it(self):
        facts = self.kg.neighbors("host:secret", subject_prefix="study:")
        self.assertEqual({f.subject for f in facts}, {"study:ps5:notes", "study:ps5:media"})
        preds = {f.predicate for f in facts}
        self.assertNotIn("has_service", preds)
        self.assertNotIn("located_in", preds)

    def test_foreign_hub_does_not_pull_own_out_of_prefix_facts(self):
        # service:vault / region:eu are objects of host:secret's own facts only;
        # scoping to study: must not surface them through the hub.
        facts = self.kg.neighbors("host:secret", subject_prefix="study:")
        objects = {f.object for f in facts}
        self.assertNotIn("service:vault", objects)
        self.assertNotIn("region:eu", objects)

    def test_depth_1_does_not_reach_intra_namespace_facts(self):
        # study:ps5:notes -covers-> heap grooming does NOT touch host:secret, so
        # a depth-1 fenced walk from the hub never reaches it.
        facts = self.kg.neighbors("host:secret", depth=1, subject_prefix="study:")
        self.assertNotIn("heap grooming", {f.object for f in facts})

    def test_walk_continues_into_namespace_at_depth_2(self):
        # depth 2: host:secret → study:ps5:notes (depth1) → its intra-namespace
        # fact study:ps5:notes -covers-> heap grooming (depth2).
        facts = self.kg.neighbors("host:secret", depth=2, subject_prefix="study:")
        self.assertIn("heap grooming", {f.object for f in facts})

    def test_no_prefix_walks_whole_graph(self):
        facts = self.kg.neighbors("host:secret", limit=50)
        subjects = {f.subject for f in facts}
        self.assertIn("host:secret", subjects)  # own facts visible unfenced
        self.assertIn("study:ps5:notes", subjects)


class SubjectPrefixEmbeddingCountsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(self._td.name)
        self.addCleanup(self.kg.close)
        self.now = time.time()
        self.f_study_a = self.kg.assert_fact("study:a", "p", "o")
        self.kg.assert_fact("study:b", "p", "o")
        self.kg.assert_fact("host:x", "p", "o")
        blob = pack_vector([1.0, 0.0, 0.0])
        # embed ONLY study:a under "mock"
        self.kg.store_embeddings([(self.f_study_a.id, blob)], "mock")

    def test_global_counts_cover_whole_graph(self):
        self.assertEqual(self.kg.embedding_counts("mock"), (3, 1, 2))

    def test_prefix_counts_scope_to_namespace(self):
        self.assertEqual(self.kg.embedding_counts("mock", subject_prefix="study:"), (2, 1, 1))

    def test_prefix_with_no_embeddings(self):
        self.assertEqual(self.kg.embedding_counts("mock", subject_prefix="host:"), (1, 0, 1))

    def test_prefix_counts_invariant(self):
        total, embedded, pending = self.kg.embedding_counts("mock", subject_prefix="study:")
        self.assertEqual(embedded + pending, total)

    def test_empty_prefix_equals_global(self):
        self.assertEqual(self.kg.embedding_counts("mock", subject_prefix=""), (3, 1, 2))
