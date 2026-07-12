"""Source-provenance (`source_ref`) tests.

Pins the provenance pointer on `salient_core.memory.kg`:

  * `source_ref` round-trips through every read path (query / get / neighbors /
    get_exact / semantic_query) and appears in `to_payload` + the __str__ badge;
  * merge semantics mirror `contradicts` — a new stamp overrides, absence
    preserves the earliest evidence anchor, and provenance never perturbs the
    noisy-OR confidence;
  * a fact asserted without a ref reads back None (back-compat), as does a
    legacy row written before the column existed;
  * the additive migration adds the column to a pre-feature on-disk DB;
  * the archive/export paths carry provenance so a restored fact keeps its
    evidence pointer;
  * the resolver seam dispatches by scheme and no-ops on an un-skinned core.

The kernel STORES and surfaces the pointer but never dereferences it — that's a
skin's job via `register_source_resolver`.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from salient_core.memory.embeddings import pack_vector
from salient_core.memory.kg import (
    _SOURCE_RESOLVERS,
    KnowledgeGraph,
    register_source_resolver,
    resolve_source_ref,
)

_REF = "archive:sha256:abc123#window_T3"


def _kg(tmp: Path) -> KnowledgeGraph:
    return KnowledgeGraph(tmp / "kg.db")


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)

    def test_assert_returns_source_ref(self):
        f = self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", source_ref=_REF)
        self.assertEqual(f.source_ref, _REF)

    def test_query_roundtrips_source_ref(self):
        self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", source_ref=_REF)
        f = self.kg.query("h:1", None, None)[0]
        self.assertEqual(f.source_ref, _REF)

    def test_get_roundtrips_source_ref(self):
        created = self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", source_ref=_REF)
        self.assertEqual(self.kg.get(created.id).source_ref, _REF)

    def test_get_exact_roundtrips_source_ref(self):
        self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", source_ref=_REF)
        self.assertEqual(self.kg.get_exact("h:1", "runs", "svc:http").source_ref, _REF)

    def test_neighbors_roundtrips_source_ref(self):
        self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", source_ref=_REF)
        f = self.kg.neighbors("h:1")[0]
        self.assertEqual(f.source_ref, _REF)

    def test_semantic_query_roundtrips_source_ref(self):
        f = self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", source_ref=_REF)
        vec = [1.0, 0.0, 0.0]
        self.kg.store_embeddings([(f.id, pack_vector(vec))], "fake-model")
        hits = self.kg.semantic_query(vec, model="fake-model", top_k=3, min_score=0.0)
        self.assertEqual(hits[0][0].source_ref, _REF)

    def test_to_payload_and_str_surface_source_ref(self):
        self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", source_ref=_REF)
        f = self.kg.query("h:1", None, None)[0]
        self.assertEqual(f.to_payload()["source_ref"], _REF)
        self.assertIn("[src]", str(f))

    def test_absent_source_ref_reads_none(self):
        self.kg.assert_fact("h:2", "runs", "svc:ssh", agent="a")
        f = self.kg.query("h:2", None, None)[0]
        self.assertIsNone(f.source_ref)
        self.assertIsNone(f.to_payload()["source_ref"])
        self.assertNotIn("[src]", str(f))

    def test_blank_source_ref_normalizes_to_none(self):
        f = self.kg.assert_fact("h:3", "runs", "svc:x", agent="a", source_ref="   ")
        self.assertIsNone(f.source_ref)


class MergeSemanticsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)

    def test_reassert_without_ref_preserves_prior(self):
        self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", source_ref=_REF)
        f = self.kg.assert_fact("h:1", "runs", "svc:http", agent="b")
        self.assertEqual(f.source_ref, _REF)  # earliest anchor kept

    def test_reassert_with_new_ref_overrides(self):
        self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", source_ref=_REF)
        newer = "tool:nmap#L47"
        f = self.kg.assert_fact("h:1", "runs", "svc:http", agent="b", source_ref=newer)
        self.assertEqual(f.source_ref, newer)

    def test_provenance_does_not_perturb_corroboration(self):
        self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", confidence=0.6, source_ref=_REF)
        f = self.kg.assert_fact("h:1", "runs", "svc:http", agent="b", confidence=0.7)
        self.assertEqual(f.corroboration_count, 2)
        self.assertAlmostEqual(f.confidence, 1 - (1 - 0.6) * (1 - 0.7))  # 0.88
        self.assertEqual(f.source_ref, _REF)


class LegacyRowTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)

    def test_legacy_null_source_ref_reads_none(self):
        # A row written directly without source_ref (mimics a pre-feature row).
        self.kg._conn.execute(
            "INSERT INTO kg_facts (subject,predicate,object,confidence,agent,ts)"
            " VALUES ('h:l','p','o',0.8,'legacy',1.0)"
        )
        self.kg._conn.commit()
        f = self.kg.get_exact("h:l", "p", "o")
        self.assertIsNone(f.source_ref)


class ArchiveFidelityTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)

    def test_export_expired_carries_source_ref(self):
        past = time.time() - 10
        self.kg.assert_fact("h:1", "runs", "svc:http", agent="a", expires_at=past, source_ref=_REF)
        exported = self.kg.export_expired()
        self.assertEqual(exported[0]["source_ref"], _REF)

    def test_export_by_subject_prefix_carries_source_ref(self):
        self.kg.assert_fact("study:proj:chunk:1", "passage", "text", agent="a", source_ref=_REF)
        exported = self.kg.export_by_subject_prefix("study:proj:")
        self.assertEqual(exported[0]["source_ref"], _REF)


class MigrationTests(unittest.TestCase):
    def test_adds_source_ref_to_pre_feature_db(self):
        """An on-disk DB created before the source_ref column existed must gain
        it on open, and old rows read back with source_ref None."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "kg.db"
            old = sqlite3.connect(str(db))
            old.executescript(
                "CREATE TABLE kg_facts ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  subject TEXT NOT NULL, predicate TEXT NOT NULL,"
                "  object TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,"
                "  agent TEXT, engagement_id TEXT, ts REAL NOT NULL);"
            )
            old.execute(
                "INSERT INTO kg_facts "
                "(subject,predicate,object,confidence,agent,engagement_id,ts) "
                "VALUES (?,?,?,?,?,?,?)",
                ("host:DC01", "role", "dc", 0.7, "legacy", "eng-old", time.time()),
            )
            old.commit()
            old.close()

            kg = KnowledgeGraph(db)
            self.addCleanup(kg.close)
            cols = {r[1] for r in kg._conn.execute("PRAGMA table_info(kg_facts)")}
            self.assertIn("source_ref", cols)
            f = kg.get_exact("host:DC01", "role", "dc")
            self.assertIsNone(f.source_ref)


class ResolverSeamTests(unittest.TestCase):
    def tearDown(self):
        _SOURCE_RESOLVERS.clear()

    def test_unskinned_core_returns_none(self):
        self.assertIsNone(resolve_source_ref(_REF))

    def test_none_and_schemeless_return_none(self):
        self.assertIsNone(resolve_source_ref(None))
        self.assertIsNone(resolve_source_ref(""))
        self.assertIsNone(resolve_source_ref("noscheme"))

    def test_registered_resolver_dispatches_by_scheme(self):
        seen = {}

        def _archive(ref: str) -> str:
            seen["ref"] = ref
            return "REDACTED-EVIDENCE"

        register_source_resolver("archive", _archive)
        self.assertEqual(resolve_source_ref(_REF), "REDACTED-EVIDENCE")
        self.assertEqual(seen["ref"], _REF)
        # A different scheme with no resolver still no-ops.
        self.assertIsNone(resolve_source_ref("tool:nmap#L47"))


if __name__ == "__main__":
    unittest.main()
