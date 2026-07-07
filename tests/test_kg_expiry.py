"""KG fact-expiration store tests (DeepSeek review N4 / S10).

Pins the `expires_at` mechanism on `salient.kg.KnowledgeGraph`:

  * additive migration adds the column to a pre-expiry on-disk DB;
  * reads (query / neighbors) hide expired facts but keep permanent +
    future ones;
  * dedup REVIVES an expired-but-unpurged triple (one row, refreshed)
    rather than inserting a duplicate;
  * purge_expired deletes only the dead rows and returns the count;
  * stats counts active facts only and reports expiring_within_7d.

Determinism without time-mocking: facts get explicit past / future
`expires_at` epochs (a past epoch is always < real now), and the
time-sensitive store methods accept an optional `now`.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from salient_core.memory.kg import KnowledgeGraph


def _kg(tmp: Path) -> KnowledgeGraph:
    return KnowledgeGraph(tmp / "kg.db")


class MigrationTests(unittest.TestCase):
    def test_alters_pre_expiry_db_in_place(self):
        """An on-disk DB created before expires_at existed must gain the
        column (and its index) on open, with old rows reading back as
        permanent (expires_at=None) — not crash on 'no such column'."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "kg.db"
            old = sqlite3.connect(str(db))
            old.executescript(
                "CREATE TABLE kg_facts ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  subject TEXT NOT NULL, predicate TEXT NOT NULL,"
                "  object TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,"
                "  agent TEXT, engagement_id TEXT, ts REAL NOT NULL);"
                "CREATE INDEX kg_subject ON kg_facts(subject);"
            )
            old.execute(
                "INSERT INTO kg_facts "
                "(subject,predicate,object,confidence,agent,engagement_id,ts) "
                "VALUES (?,?,?,?,?,?,?)",
                ("host:DC01", "role", "domain_controller", 1.0, "legacy", "eng-old", time.time()),
            )
            old.commit()
            old.close()

            kg = self.addCleanup_and_open(db)
            cols = {r[1] for r in kg._conn.execute("PRAGMA table_info(kg_facts)")}
            self.assertIn("expires_at", cols)
            idx = {r[1] for r in kg._conn.execute("PRAGMA index_list(kg_facts)")}
            self.assertIn("kg_expires", idx)
            facts = kg.query("host:DC01", None, None, limit=5)
            self.assertEqual(len(facts), 1)
            self.assertIsNone(facts[0].expires_at)

    def test_open_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "kg.db"
            KnowledgeGraph(db).close()
            kg = KnowledgeGraph(db)  # second open must not raise
            self.addCleanup(kg.close)
            kg.assert_fact("a", "b", "c")
            self.assertEqual(len(kg.query("a", None, None)), 1)

    def addCleanup_and_open(self, db: Path) -> KnowledgeGraph:
        kg = KnowledgeGraph(db)
        self.addCleanup(kg.close)
        return kg


class ReadFilterTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)
        self.now = time.time()

    def test_query_hides_expired_keeps_permanent_and_future(self):
        self.kg.assert_fact("h:perm", "role", "x", expires_at=None)
        self.kg.assert_fact("h:future", "role", "x", expires_at=self.now + 3600)
        self.kg.assert_fact("h:dead", "role", "x", expires_at=self.now - 1)
        got = {f.subject for f in self.kg.query(None, "role", None, limit=50)}
        self.assertEqual(got, {"h:perm", "h:future"})

    def test_neighbors_hides_expired(self):
        self.kg.assert_fact("host:A", "talks_to", "host:B", expires_at=self.now - 1)
        self.kg.assert_fact("host:A", "talks_to", "host:C", expires_at=None)
        got = {f.object for f in self.kg.neighbors("host:A")}
        self.assertEqual(got, {"host:C"})


class DedupeReviveTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)

    def test_reassert_revives_expired_row_no_duplicate(self):
        now = time.time()
        self.kg.assert_fact("host:DC01", "role", "dc", expires_at=now - 1)
        # Gone from reads while expired…
        self.assertEqual(self.kg.query("host:DC01", None, None), [])
        # …re-assert with a fresh TTL revives the SAME row.
        self.kg.assert_fact("host:DC01", "role", "dc", expires_at=now + 3600)
        active = self.kg.query("host:DC01", None, None)
        self.assertEqual(len(active), 1)
        # Exactly one physical row — revived, not duplicated.
        raw = self.kg._conn.execute(
            "SELECT COUNT(*) FROM kg_facts WHERE subject='host:DC01'"
        ).fetchone()[0]
        self.assertEqual(raw, 1)


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)

    def test_purge_removes_only_expired_returns_count(self):
        now = time.time()
        self.kg.assert_fact("h:perm", "p", "o", expires_at=None)
        self.kg.assert_fact("h:future", "p", "o", expires_at=now + 3600)
        self.kg.assert_fact("h:d1", "p", "o", expires_at=now - 1)
        self.kg.assert_fact("h:d2", "p", "o", expires_at=now - 100)
        removed = self.kg.purge_expired()
        self.assertEqual(removed, 2)
        remaining = {r[0] for r in self.kg._conn.execute("SELECT subject FROM kg_facts")}
        self.assertEqual(remaining, {"h:perm", "h:future"})

    def test_purge_accepts_explicit_now(self):
        base = 1_000_000.0
        self.kg.assert_fact("h:a", "p", "o", expires_at=base + 10)
        # Nothing expired at base; everything expired well after.
        self.assertEqual(self.kg.purge_expired(now=base), 0)
        self.assertEqual(self.kg.purge_expired(now=base + 11), 1)


class StatsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = _kg(Path(self._td.name))
        self.addCleanup(self.kg.close)

    def test_stats_counts_active_only_and_expiring_soon(self):
        now = time.time()
        self.kg.assert_fact("h:perm", "role", "x", expires_at=None)
        self.kg.assert_fact("h:soon", "role", "x", expires_at=now + 2 * 86400)  # within 7d
        self.kg.assert_fact("h:later", "role", "x", expires_at=now + 30 * 86400)  # outside 7d
        self.kg.assert_fact("h:dead", "role", "x", expires_at=now - 1)  # expired
        st = self.kg.stats()
        # h:dead excluded from the active total.
        self.assertEqual(st["total_facts"], 3)
        self.assertEqual(st["expiring_within_7d"], 1)
        self.assertIn("role", {tp["predicate"] for tp in st["top_predicates"]})


if __name__ == "__main__":
    unittest.main()
