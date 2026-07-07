"""ContextStore retention — per-agent ring buffer + engagement_id stamp.

Two halves:

  Schema half: the additive engagement_id column on the events table.
  Migration must work on fresh DBs (CREATE TABLE) and on pre-feature
  DBs (ALTER TABLE ADD COLUMN). Old rows stay NULL; new rows carry
  the constructor-supplied id.

  Retention half: per-agent ring buffer caps the events table. Without
  it the bus grows unbounded across long engagements (verified in the
  Explore audit — no DELETE / VACUUM / partitioning before this
  change). Pruning is periodic (every N inserts) rather than on every
  insert, so the hot write path stays cheap.

The invariants pinned here are operator-facing:
  - storage growth IS bounded by `events_cap_per_agent`
  - the bound is per-agent, so a noisy agent doesn't crowd out a quiet
    agent's history
  - `events_cap_per_agent <= 0` is the escape hatch for "I want full
    history" (long red-team engagements, replay diagnostics)
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from salient_core.bus import ContextStore


def _count_events(store: ContextStore, agent: str | None = None) -> int:
    assert store._conn is not None
    if agent is None:
        return store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return store._conn.execute("SELECT COUNT(*) FROM events WHERE agent=?", (agent,)).fetchone()[0]


class EngagementIdSchemaTests(unittest.TestCase):
    """Schema-level pins for the additive engagement_id column."""

    def test_engagement_id_column_added_on_fresh_db(self):
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "t.db", engagement_id="op-2026")
            cols = {row[1] for row in store._conn.execute("PRAGMA table_info(events)")}
            self.assertIn("engagement_id", cols, "events.engagement_id missing on fresh DB")
            # And the supporting index exists.
            indexes = {row[1] for row in store._conn.execute("PRAGMA index_list(events)")}
            self.assertIn("events_by_engagement_ts", indexes)

    def test_engagement_id_column_added_on_pre_existing_db(self):
        """Pre-feature DB: schema lacks engagement_id. The migration
        should add it via ALTER TABLE without touching existing rows."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.db"
            # Hand-build a DB matching the OLD schema (pre engagement_id).
            conn = sqlite3.connect(str(db_path))
            conn.executescript("""
                CREATE TABLE events (
                    rowid     INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        REAL NOT NULL,
                    agent     TEXT NOT NULL,
                    kind      TEXT NOT NULL,
                    job_id    INTEGER,
                    tool      TEXT,
                    source    TEXT,
                    recipient TEXT,
                    content   TEXT NOT NULL
                );
            """)
            conn.execute(
                "INSERT INTO events (ts, agent, kind, content) VALUES (?, ?, ?, ?)",
                (1000.0, "legacy-agent", "text", '"hello"'),
            )
            conn.commit()
            conn.close()
            # Open via ContextStore — should run the migration.
            store = ContextStore(db_path, engagement_id="op-now")
            cols = {row[1] for row in store._conn.execute("PRAGMA table_info(events)")}
            self.assertIn(
                "engagement_id", cols, "ALTER TABLE ADD COLUMN must run on pre-feature DB"
            )

    def test_record_event_stamps_engagement_id(self):
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "t.db", engagement_id="op-2026")
            store.record_event("alpha", "text", {"msg": "hi"})
            row = store._conn.execute(
                "SELECT engagement_id FROM events WHERE agent='alpha'"
            ).fetchone()
            self.assertEqual(row[0], "op-2026")

    def test_record_event_stamps_null_when_no_engagement(self):
        """No engagement → column carries NULL. Engagement-close DELETE
        on engagement_id=? will skip these (the right default for
        rows that pre-date / weren't scoped to any engagement)."""
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "t.db")  # no engagement_id
            store.record_event("alpha", "text", {"msg": "hi"})
            row = store._conn.execute(
                "SELECT engagement_id FROM events WHERE agent='alpha'"
            ).fetchone()
            self.assertIsNone(row[0])

    def test_existing_rows_get_null_engagement_id_on_migration(self):
        """Migration must leave old rows alone (NULL). Backfilling them
        with the CURRENT engagement_id would mis-label history."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript("""
                CREATE TABLE events (
                    rowid     INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        REAL NOT NULL,
                    agent     TEXT NOT NULL,
                    kind      TEXT NOT NULL,
                    job_id    INTEGER,
                    tool      TEXT,
                    source    TEXT,
                    recipient TEXT,
                    content   TEXT NOT NULL
                );
            """)
            conn.execute(
                "INSERT INTO events (ts, agent, kind, content) VALUES (?, ?, ?, ?)",
                (1000.0, "legacy-agent", "text", '"pre-feature"'),
            )
            conn.commit()
            conn.close()
            store = ContextStore(db_path, engagement_id="op-2026")
            row = store._conn.execute(
                "SELECT engagement_id, content FROM events WHERE agent='legacy-agent'"
            ).fetchone()
            self.assertIsNone(
                row[0],
                "pre-feature row must keep NULL engagement_id; "
                "backfilling with the current id would mis-label",
            )
            self.assertEqual(row[1], '"pre-feature"')


class RingBufferPruneTests(unittest.TestCase):
    """Per-agent ring-buffer cap. The invariant: after the prune sweep,
    no agent has more than `events_cap_per_agent` rows. The newest
    rows (by ts) survive; the oldest get evicted."""

    def test_prune_keeps_most_recent_n_per_agent(self):
        with tempfile.TemporaryDirectory() as td:
            # cap=10, check every 5 inserts → after 50 inserts of one
            # agent we should see exactly 10 surviving rows.
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=10,
                prune_check_interval=5,
            )
            for i in range(50):
                # Pass ts explicitly so the ordering is unambiguous
                # — time.time() within a tight loop can produce ties.
                store.record_event("alpha", "text", {"i": i}, ts=1000.0 + i)
            self.assertEqual(_count_events(store, "alpha"), 10)

    def test_prune_is_independent_per_agent(self):
        """A noisy agent shouldn't sweep a quiet agent's history."""
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=10,
                prune_check_interval=5,
            )
            for i in range(50):
                store.record_event("noisy", "text", {"i": i}, ts=1000.0 + i)
            for i in range(3):
                store.record_event("quiet", "text", {"i": i}, ts=2000.0 + i)
            # Force a final sweep so the counter slop from periodic
            # pruning doesn't leave noisy a few above cap. Tests for
            # the periodic-vs-immediate behavior live in
            # test_prune_fires_after_check_interval.
            store._prune_events()
            self.assertEqual(_count_events(store, "noisy"), 10)
            # Quiet agent only had 3 events; cap=10 → all 3 survive.
            self.assertEqual(_count_events(store, "quiet"), 3)

    def test_prune_no_op_when_under_cap(self):
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=100,
                prune_check_interval=1,  # check on every insert
            )
            for i in range(5):
                store.record_event("alpha", "text", {"i": i})
            # 5 < cap=100 → no deletions, all 5 should survive.
            self.assertEqual(_count_events(store, "alpha"), 5)

    def test_prune_fires_after_check_interval(self):
        """Pruning is periodic by design — it only fires when the
        in-memory counter hits the check interval. Below the interval,
        a single agent CAN temporarily exceed the cap (which is
        fine — the operator pays a few extra rows for write-path
        cheapness)."""
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=10,
                prune_check_interval=1000,
            )
            # 50 inserts, check interval is 1000 → no prune fires yet,
            # all 50 rows remain.
            for i in range(50):
                store.record_event("alpha", "text", {"i": i}, ts=1000.0 + i)
            self.assertEqual(
                _count_events(store, "alpha"), 50, "no prune should fire before check interval"
            )
            # Manual call cuts down to cap.
            deleted = store._prune_events()
            self.assertEqual(deleted, 40)
            self.assertEqual(_count_events(store, "alpha"), 10)

    def test_prune_cap_zero_disables(self):
        """Escape hatch — operators who want full history pass 0."""
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=0,
                prune_check_interval=1,
            )
            for i in range(100):
                store.record_event("alpha", "text", {"i": i})
            self.assertEqual(
                _count_events(store, "alpha"), 100, "cap=0 must disable pruning entirely"
            )
            # Also: direct call is a no-op.
            self.assertEqual(store._prune_events(), 0)

    def test_prune_preserves_order_within_agent(self):
        """The newest rows (by ts) must be the survivors. Pins that
        we ORDER BY ts ASC LIMIT (n - cap), not the reverse."""
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=5,
                prune_check_interval=1,
            )
            for i in range(20):
                store.record_event("alpha", "text", {"i": i}, ts=1000.0 + i)
            # Survivors should be ts 1015..1019 — the last 5 inserts.
            rows = store._conn.execute(
                "SELECT ts FROM events WHERE agent='alpha' ORDER BY ts ASC"
            ).fetchall()
            ts_values = [r[0] for r in rows]
            self.assertEqual(ts_values, [1015.0, 1016.0, 1017.0, 1018.0, 1019.0])

    def test_prune_handles_empty_table(self):
        """No agents have ever fired → prune is a no-op, no error."""
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=10,
            )
            self.assertEqual(store._prune_events(), 0)


class HotPathContractTests(unittest.TestCase):
    """Tests that the retention work didn't break the existing
    record_event / query_events contract that the daemon relies on."""

    def test_query_events_still_works_post_prune(self):
        """The bus pane / `salientctl events query` reads via
        query_events. Pruning must not break that path."""
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=5,
                prune_check_interval=1,
            )
            for i in range(20):
                store.record_event("alpha", "text", {"i": i}, ts=1000.0 + i)
            events = store.query_events(agent="alpha", limit=100)
            self.assertEqual(len(events), 5)
            # query_events returns oldest→newest within the result set;
            # after pruning, the surviving range is 1015..1019.
            self.assertEqual(events[0]["ts"], 1015.0)
            self.assertEqual(events[-1]["ts"], 1019.0)

    def test_record_event_failures_dont_crash_caller(self):
        """record_event is fire-and-forget — any DB error swallowed.
        Pruning logic shouldn't accidentally raise either."""
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=10,
                prune_check_interval=1,
            )

            # Pass a non-JSON-serializable value to trigger the
            # fallback payload path; record_event must not raise.
            class Weird:
                pass

            store.record_event("alpha", "text", Weird())  # must not raise
            # And the row landed (via the {"_repr": ...} fallback).
            self.assertEqual(_count_events(store, "alpha"), 1)

    def test_concurrent_inserts_keep_cap_within_check_interval_slop(self):
        """Threaded inserts: with cap=20 and check interval=1, we
        should see at most cap rows once the dust settles. SQLite's
        check_same_thread=False + the RLock guarantee serialization;
        pruning is part of the locked section so no agent can race
        past the ceiling between count and DELETE."""
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(
                Path(td) / "t.db",
                engagement_id="t",
                events_cap_per_agent=20,
                prune_check_interval=1,
            )

            def hammer(start: int) -> None:
                for i in range(50):
                    store.record_event(
                        "alpha",
                        "text",
                        {"i": start + i},
                        ts=1000.0 + start + i,
                    )

            threads = [threading.Thread(target=hammer, args=(i * 100,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # Final sweep — every insert checks, last one ends at cap.
            self.assertEqual(_count_events(store, "alpha"), 20)


if __name__ == "__main__":
    unittest.main()
