"""Confidence-weighted corroboration store tests.

Pins the corroboration mechanism on `salient.kg.KnowledgeGraph`:

  * independent agents asserting the SAME triple pool their confidences via
    noisy-OR (1 - Π(1 - cᵢ)) and the fact records who corroborated it;
  * the SAME agent re-asserting never double-counts (per-agent max only);
  * a fact asserted once keeps its EXACT confidence (back-compat);
  * the combined value is capped at 0.99, but an explicit 1.0 stays certain;
  * adding agents is monotonic — corroboration never lowers confidence;
  * legacy NULL-map rows read as a single-agent fact and corroborate cleanly;
  * the additive migration adds the columns to a pre-feature on-disk DB;
  * a corrupt corroborators blob falls back without raising;
  * `contradicts` is stored on the row + surfaced on the Fact / in __str__.

Float comparisons use assertAlmostEqual — noisy-OR introduces representation
error, and the stored value only needs to be right to ~1e-9.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from salient_core.memory.kg import KnowledgeGraph, _combined_confidence, _noisy_or


def _kg(tmp: Path) -> KnowledgeGraph:
    return KnowledgeGraph(tmp / "kg.db")


class FormulaTests(unittest.TestCase):
    def test_noisy_or_two_agents(self):
        self.assertAlmostEqual(_noisy_or([0.6, 0.7]), 0.88)

    def test_noisy_or_clamps_out_of_range(self):
        # 1.5 clamps to 1.0 → certainty; -0.2 clamps to 0.0 → no effect.
        self.assertEqual(_noisy_or([1.5]), 1.0)
        self.assertAlmostEqual(_noisy_or([0.5, -0.2]), 0.5)

    def test_combined_single_agent_is_exact(self):
        # No float drift for an asserted-once fact.
        self.assertEqual(_combined_confidence({"a": 0.42}), 0.42)

    def test_combined_explicit_one_stays_certain(self):
        self.assertEqual(_combined_confidence({"a": 1.0, "b": 0.5}), 1.0)

    def test_combined_caps_at_099(self):
        self.assertEqual(_combined_confidence({"a": 0.9, "b": 0.9, "c": 0.9}), 0.99)

    def test_combined_empty_is_zero(self):
        self.assertEqual(_combined_confidence({}), 0.0)


class CorroborationStoreTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)

    def test_two_distinct_agents_boost_and_flag(self):
        self.kg.assert_fact("host:1", "has_port", "22", confidence=0.6, agent="alpha")
        f = self.kg.assert_fact("host:1", "has_port", "22", confidence=0.7, agent="beta")
        self.assertAlmostEqual(f.confidence, 0.88)
        self.assertEqual(f.corroboration_count, 2)
        self.assertTrue(f.corroborated)
        self.assertEqual(set(f.corroborators), {"alpha", "beta"})

    def test_same_agent_reassert_no_double_count(self):
        self.kg.assert_fact("host:1", "p", "o", confidence=0.6, agent="alpha")
        f = self.kg.assert_fact("host:1", "p", "o", confidence=0.6, agent="alpha")
        self.assertAlmostEqual(f.confidence, 0.6)  # not 0.84
        self.assertEqual(f.corroboration_count, 1)
        self.assertFalse(f.corroborated)
        # A higher re-assert by the same agent raises ITS entry (max), still 1.
        f2 = self.kg.assert_fact("host:1", "p", "o", confidence=0.8, agent="alpha")
        self.assertAlmostEqual(f2.confidence, 0.8)
        self.assertEqual(f2.corroboration_count, 1)

    def test_get_by_id_roundtrips_with_corroboration(self):
        f = self.kg.assert_fact("host:9", "has_port", "443", confidence=0.7, agent="alpha")
        got = self.kg.get(f.id)
        self.assertIsNotNone(got)
        self.assertEqual((got.subject, got.predicate, got.object), ("host:9", "has_port", "443"))
        self.assertEqual(set(got.corroborators), {"alpha"})

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.kg.get(999_999))

    def test_get_after_close_returns_none(self):
        f = self.kg.assert_fact("host:9", "p", "o", confidence=0.5, agent="a")
        self.kg.close()
        self.assertIsNone(self.kg.get(f.id))

    def test_corroborated_flips_at_two(self):
        f1 = self.kg.assert_fact("h", "p", "o", confidence=0.5, agent="a")
        self.assertFalse(f1.corroborated)
        f2 = self.kg.assert_fact("h", "p", "o", confidence=0.5, agent="b")
        self.assertTrue(f2.corroborated)

    def test_explicit_one_stays_certain(self):
        self.kg.assert_fact("h", "p", "o", confidence=1.0, agent="a")
        f = self.kg.assert_fact("h", "p", "o", confidence=0.5, agent="b")
        self.assertEqual(f.confidence, 1.0)
        self.assertEqual(f.corroboration_count, 2)

    def test_single_assert_keeps_exact_confidence(self):
        f = self.kg.assert_fact("h", "p", "o", confidence=0.42, agent="a")
        self.assertEqual(f.confidence, 0.42)
        self.assertFalse(f.corroborated)

    def test_monotonic_never_lowers(self):
        c0 = self.kg.assert_fact("h", "p", "o", confidence=0.3, agent="a").confidence
        c1 = self.kg.assert_fact("h", "p", "o", confidence=0.1, agent="b").confidence
        self.assertGreaterEqual(c1, c0)

    def test_query_roundtrips_flag_and_str(self):
        self.kg.assert_fact("host:1", "has_port", "22", confidence=0.6, agent="a")
        self.kg.assert_fact("host:1", "has_port", "22", confidence=0.7, agent="b")
        got = self.kg.query("host:1", None, None)[0]
        self.assertTrue(got.corroborated)
        self.assertEqual(got.corroboration_count, 2)
        self.assertIn("[corroborated ×2]", str(got))

    def test_single_agent_fact_has_no_badge(self):
        self.kg.assert_fact("host:9", "has_port", "22", confidence=0.6, agent="a")
        got = self.kg.query("host:9", None, None)[0]
        self.assertNotIn("corroborated", str(got))

    def test_corrupt_corroborators_blob_falls_back(self):
        self.kg.assert_fact("h", "p", "o", confidence=0.5, agent="a")
        self.kg._conn.execute("UPDATE kg_facts SET corroborators='{bad' WHERE subject='h'")
        self.kg._conn.commit()
        got = self.kg.query("h", None, None)[0]  # must not raise
        self.assertEqual(got.corroboration_count, 1)

    def test_contradiction_persisted_and_surfaced(self):
        f = self.kg.assert_fact(
            "host:2", "os", "linux", confidence=0.5, agent="g", contradicts="host:2:windows"
        )
        self.assertEqual(f.contradiction, "host:2:windows")
        # Durable: a fresh read shows it + the __str__ flag.
        got = self.kg.get_exact("host:2", "os", "linux")
        self.assertEqual(got.contradiction, "host:2:windows")
        self.assertIn("CONTRADICTION FLAGGED", str(got))

    def test_plain_reassert_preserves_prior_contradiction(self):
        self.kg.assert_fact(
            "host:2", "os", "linux", confidence=0.5, agent="g", contradicts="host:2:windows"
        )
        # A later plain re-assert (no contradicts) must not clear the flag.
        self.kg.assert_fact("host:2", "os", "linux", confidence=0.9, agent="h")
        got = self.kg.get_exact("host:2", "os", "linux")
        self.assertEqual(got.contradiction, "host:2:windows")

    def test_to_payload_exposes_corroboration(self):
        self.kg.assert_fact("h", "p", "o", confidence=0.6, agent="a")
        self.kg.assert_fact("h", "p", "o", confidence=0.7, agent="b")
        pl = self.kg.query("h", None, None)[0].to_payload()
        self.assertEqual(pl["corroboration_count"], 2)
        self.assertTrue(pl["corroborated"])
        self.assertEqual(set(pl["corroborators"]), {"a", "b"})


class LegacyRowTests(unittest.TestCase):
    """A row written before the corroborators column existed (NULL map) must
    read as a single-agent fact, and a second distinct agent must corroborate
    it using the EXISTING row's agent as the legacy attribution."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)

    def test_legacy_null_map_reads_as_single_agent(self):
        self.kg._conn.execute(
            "INSERT INTO kg_facts (subject,predicate,object,confidence,agent,ts)"
            " VALUES ('h:l','p','o',0.8,'legacy',1.0)"
        )
        self.kg._conn.commit()
        f = self.kg.get_exact("h:l", "p", "o")
        self.assertEqual(f.corroboration_count, 1)
        self.assertFalse(f.corroborated)
        self.assertEqual(f.confidence, 0.8)

    def test_legacy_row_corroborated_by_new_agent(self):
        self.kg._conn.execute(
            "INSERT INTO kg_facts (subject,predicate,object,confidence,agent,ts)"
            " VALUES ('h:l','p','o',0.8,'legacy',1.0)"
        )
        self.kg._conn.commit()
        f = self.kg.assert_fact("h:l", "p", "o", confidence=0.5, agent="newby")
        self.assertEqual(f.corroboration_count, 2)
        self.assertEqual(set(f.corroborators), {"legacy", "newby"})
        self.assertAlmostEqual(f.confidence, 1 - (1 - 0.8) * (1 - 0.5))  # 0.9


class MigrationTests(unittest.TestCase):
    def test_adds_columns_to_pre_feature_db(self):
        """An on-disk DB created before the corroboration columns existed must
        gain `corroborators` + `contradiction` on open, old rows reading back
        as single-agent facts."""
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
            self.assertIn("corroborators", cols)
            self.assertIn("contradiction", cols)
            f = kg.get_exact("host:DC01", "role", "dc")
            self.assertEqual(f.confidence, 0.7)
            self.assertEqual(f.corroboration_count, 1)
            self.assertFalse(f.corroborated)


if __name__ == "__main__":
    unittest.main()
