"""T3.1 spine — the correlation_id column is additive-migratable and round-trips.

Pins two contracts the reconstruct spine depends on:

  1. A pre-spine DB (jobs/questions/approval_bypass with NO correlation_id
     column) is migrated in place on open — the column and its index appear,
     old rows read back NULL, and the existing data survives. This mirrors the
     established additive-migration precedent (`_migrate_jobs_prompt_sha`, etc.).
  2. On a fresh DB, a correlation_id written through record_job / record_question
     / record_bypass reads back on the joinable path (load_recent_jobs,
     load_pending_questions, load_bypasses / raw query).

If a future change drops the column or forgets to thread it, this breaks.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from salient_core.bus import ContextStore

_TABLES = ("jobs", "questions", "approval_bypass")


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


class CorrelationIdMigrationTests(unittest.TestCase):
    def test_pre_spine_db_is_migrated_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.db"
            # Build an OLD-shape DB by hand: the three tables WITHOUT the
            # correlation_id column, plus a legacy row in each.
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE jobs (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL,
                    job_id INTEGER NOT NULL, submitted_at REAL NOT NULL,
                    started_at REAL, finished_at REAL, prompt TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '', error TEXT, prompt_sha TEXT);
                CREATE TABLE questions (
                    id INTEGER PRIMARY KEY, agent TEXT NOT NULL, text TEXT NOT NULL,
                    job_id INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'operator',
                    asked_at REAL NOT NULL, answered_at REAL, answer TEXT,
                    answer_job_id INTEGER, answered_by TEXT);
                CREATE TABLE approval_bypass (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                    caller TEXT NOT NULL, target TEXT NOT NULL, gate TEXT NOT NULL,
                    prompt TEXT, trust_scope TEXT, engagement_id TEXT);
                """
            )
            conn.execute(
                "INSERT INTO jobs (agent, job_id, submitted_at, prompt) VALUES ('a', 1, 1.0, 'old')"
            )
            conn.execute(
                "INSERT INTO questions (id, agent, text, asked_at) VALUES (1, 'a', 'q', 1.0)"
            )
            conn.execute(
                "INSERT INTO approval_bypass (ts, caller, target, gate) "
                "VALUES (1.0, 'a', 'b', 'delegation')"
            )
            conn.commit()
            conn.close()

            for t in _TABLES:
                with sqlite3.connect(path) as c:
                    self.assertNotIn("correlation_id", _cols(c, t), f"{t} pre-state")

            # Opening a ContextStore runs the additive migration.
            store = ContextStore(path, events_cap_per_agent=0)
            try:
                self.assertFalse(store.degraded, "migration must not degrade the store")
                with sqlite3.connect(path) as c:
                    for t in _TABLES:
                        self.assertIn("correlation_id", _cols(c, t), f"{t} post-migrate")
                    # legacy rows survive, correlation_id NULL (they predate it)
                    self.assertEqual(
                        c.execute("SELECT correlation_id FROM jobs WHERE job_id=1").fetchone()[0],
                        None,
                    )
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                    # the per-correlation index was created
                    idx = {
                        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")
                    }
                    for t in _TABLES:
                        self.assertIn(f"{t}_by_correlation", idx)
            finally:
                store.close()

    def test_correlation_id_round_trips_on_fresh_db(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db", engagement_id="op-x")
            try:
                cid = "op-x:3:7"
                store.record_job(
                    agent="scout",
                    job_id=1,
                    prompt="p",
                    submitted_at=1.0,
                    started_at=1.0,
                    finished_at=2.0,
                    result="ok",
                    error=None,
                    correlation_id=cid,
                )
                store.record_question(
                    qid=1,
                    agent="scout",
                    text="q",
                    job_id=1,
                    kind="operator",
                    asked_at=1.0,
                    correlation_id=cid,
                )
                store.record_bypass(
                    "scout",
                    "sub",
                    "delegation",
                    "snip",
                    "all",
                    correlation_id=cid,
                )

                jobs = store.load_recent_jobs("scout")
                self.assertEqual(jobs[0]["correlation_id"], cid)
                pend = store.load_pending_questions()
                self.assertEqual(pend[0]["correlation_id"], cid)
                byp = store.load_bypasses()
                self.assertEqual(byp[0]["correlation_id"], cid)
            finally:
                store.close()

    def test_backfill_recovers_pre_fix_question_correlations(self) -> None:
        # Questions written before the add_question fix stored a valid job_id
        # but a NULL correlation_id. The boot back-fill recovers the join from
        # the jobs anchor — but ONLY when it's unambiguous.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.db"
            store = ContextStore(path, engagement_id="op")
            try:
                store.record_job(
                    agent="scout",
                    job_id=1,
                    prompt="p1",
                    submitted_at=1.0,
                    started_at=1.0,
                    finished_at=2.0,
                    result="",
                    error=None,
                    correlation_id="op:1:1",
                )
                store.record_job(
                    agent="scout",
                    job_id=2,
                    prompt="p2",
                    submitted_at=3.0,
                    started_at=3.0,
                    finished_at=4.0,
                    result="",
                    error=None,
                    correlation_id="op:1:2",
                )
                # recoverable: NULL correlation, valid job_id=1
                store.record_question(
                    qid=1,
                    agent="scout",
                    text="q1",
                    job_id=1,
                    kind="operator",
                    asked_at=1.5,
                    correlation_id=None,
                )
                # idle (job_id=0) → must stay NULL (belongs to no turn)
                store.record_question(
                    qid=2,
                    agent="scout",
                    text="idle",
                    job_id=0,
                    kind="operator",
                    asked_at=1.6,
                    correlation_id=None,
                )
                # ambiguous: a 2nd job row reuses (scout, job 2) under a new epoch
                # (as a reset would) → (scout,2) now maps to two correlations.
                assert store._conn is not None
                store._conn.execute(
                    "INSERT INTO jobs (agent, job_id, submitted_at, prompt, result, "
                    "correlation_id) VALUES ('scout', 2, 5.0, 'p2b', '', 'op:2:1')"
                )
                store._conn.commit()
                store.record_question(
                    qid=3,
                    agent="scout",
                    text="q3",
                    job_id=2,
                    kind="operator",
                    asked_at=5.5,
                    correlation_id=None,
                )
            finally:
                store.close()

            # Reopen → the boot migration runs the back-fill.
            store2 = ContextStore(path, engagement_id="op")
            try:
                rows = {r["id"]: r for r in store2.load_pending_questions()}
                self.assertEqual(rows[1]["correlation_id"], "op:1:1", "recovered")
                self.assertIsNone(rows[2]["correlation_id"], "idle stays NULL")
                self.assertIsNone(rows[3]["correlation_id"], "ambiguous stays NULL")

                # idempotent: a second pass changes nothing and doesn't degrade.
                store2._backfill_question_correlation_id()
                self.assertFalse(store2.degraded)
                rows2 = {r["id"]: r for r in store2.load_pending_questions()}
                self.assertEqual(rows2[1]["correlation_id"], "op:1:1")
                self.assertIsNone(rows2[3]["correlation_id"])
            finally:
                store2.close()


if __name__ == "__main__":
    unittest.main()
