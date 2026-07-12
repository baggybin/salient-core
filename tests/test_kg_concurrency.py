"""Connection-discipline tests for `KnowledgeGraph`.

Pins the one-writer / snapshot-isolated-readers model introduced for the
shared-server promotion:

  * a mutating method that raises mid-transaction ROLLS BACK — it can never
    leak an open transaction (and half a write) to the next lock-holder;
  * after such a failure the next writer commits ONLY its own work;
  * reads run on per-thread READ-ONLY connections: they see committed data,
    cannot write, and are not serialized by the writer lock;
  * a reader thread holding a long fetch does not block a concurrent writer;
  * close() closes tracked read connections and stays idempotent, and reads
    after close degrade exactly as before (get() → None).

The mid-write failure is induced by monkeypatching json.dumps used inside
assert_fact's UPDATE path — the exception fires after the dedupe SELECT,
inside the transaction, exactly the window the old manual-commit code left
dirty.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from salient_core.memory import kg as kg_mod
from salient_core.memory.kg import KnowledgeGraph


def _kg(tmp: Path) -> KnowledgeGraph:
    return KnowledgeGraph(tmp / "kg.db")


class WriteTransactionTests(unittest.TestCase):
    def test_failed_update_rolls_back_and_leaves_no_open_txn(self):
        with tempfile.TemporaryDirectory() as td:
            kg = _kg(Path(td))
            kg.assert_fact("h", "p", "o", confidence=0.4, agent="a1")
            # Corroborating assert takes the UPDATE path, which serializes the
            # corroborator map via json.dumps — make that blow up mid-txn.
            with mock.patch.object(kg_mod.json, "dumps", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    kg.assert_fact("h", "p", "o", confidence=0.9, agent="a2")
            # The failed write rolled back: no txn left open on the shared
            # write connection, and the row is untouched.
            self.assertFalse(kg._conn.in_transaction)
            f = kg.get_exact("h", "p", "o")
            self.assertIsNotNone(f)
            self.assertAlmostEqual(f.confidence, 0.4)
            self.assertEqual(f.corroborators, {"a1": 0.4})
            kg.close()

    def test_next_writer_commits_only_its_own_work_after_failure(self):
        with tempfile.TemporaryDirectory() as td:
            kg = _kg(Path(td))
            kg.assert_fact("h", "p", "o", confidence=0.4, agent="a1")
            with mock.patch.object(kg_mod.json, "dumps", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    kg.assert_fact("h", "p", "o", confidence=0.9, agent="a2")
            # A subsequent, unrelated write from another thread succeeds and
            # commits exactly one row — not the orphaned half-update too.
            err: list[BaseException] = []

            def other_write() -> None:
                try:
                    kg.assert_fact("h2", "p2", "o2", agent="a3")
                except BaseException as exc:  # pragma: no cover - fail loud
                    err.append(exc)

            t = threading.Thread(target=other_write)
            t.start()
            t.join(timeout=10)
            self.assertFalse(t.is_alive())
            self.assertEqual(err, [])
            self.assertIsNotNone(kg.get_exact("h2", "p2", "o2"))
            f = kg.get_exact("h", "p", "o")
            self.assertAlmostEqual(f.confidence, 0.4)  # a2 never landed
            kg.close()

    def test_delete_and_purge_still_report_rowcounts(self):
        with tempfile.TemporaryDirectory() as td:
            kg = _kg(Path(td))
            f = kg.assert_fact("h", "p", "o")
            kg.assert_fact("h", "p", "gone", expires_at=time.time() - 1)
            self.assertEqual(kg.purge_expired(), 1)
            self.assertTrue(kg.delete(f.id))
            self.assertFalse(kg.delete(f.id))
            kg.close()


class ReadConnectionTests(unittest.TestCase):
    def test_reads_see_committed_writes(self):
        with tempfile.TemporaryDirectory() as td:
            kg = _kg(Path(td))
            kg.assert_fact("host:a", "has_service", "svc:web", agent="a1")
            self.assertEqual(len(kg.query(subject="host:a")), 1)
            self.assertIsNotNone(kg.get_exact("host:a", "has_service", "svc:web"))
            self.assertEqual(kg.stats()["total_facts"], 1)
            kg.close()

    def test_read_connection_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            kg = _kg(Path(td))
            kg.assert_fact("h", "p", "o")
            conn = kg._read_conn()
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM kg_facts")
            kg.close()

    def test_read_connections_are_per_thread_and_tracked(self):
        with tempfile.TemporaryDirectory() as td:
            kg = _kg(Path(td))
            kg.assert_fact("h", "p", "o")
            main_conn = kg._read_conn()
            self.assertIs(kg._read_conn(), main_conn)  # cached per thread
            seen: list[object] = []

            def reader() -> None:
                seen.append(kg._read_conn())
                kg.query(subject="h")

            t = threading.Thread(target=reader)
            t.start()
            t.join(timeout=10)
            self.assertIsNot(seen[0], main_conn)
            self.assertEqual(len(kg._read_conns), 2)
            kg.close()

    def test_slow_reader_does_not_block_writer(self):
        """A reader thread sitting inside a read (no writer lock held) must
        not stop a concurrent assert_fact from committing."""
        with tempfile.TemporaryDirectory() as td:
            kg = _kg(Path(td))
            for i in range(50):
                kg.assert_fact(f"host:{i}", "p", "o")
            reader_in = threading.Event()
            release = threading.Event()
            wrote = threading.Event()

            def slow_reader() -> None:
                conn = kg._read_conn()
                cur = conn.execute("SELECT * FROM kg_facts")
                cur.fetchone()  # hold an open read snapshot
                reader_in.set()
                release.wait(timeout=10)
                cur.fetchall()

            def writer() -> None:
                reader_in.wait(timeout=10)
                kg.assert_fact("host:new", "p", "o")
                wrote.set()

            tr = threading.Thread(target=slow_reader)
            tw = threading.Thread(target=writer)
            tr.start()
            tw.start()
            self.assertTrue(wrote.wait(timeout=10), "writer blocked by reader")
            release.set()
            tr.join(timeout=10)
            tw.join(timeout=10)
            self.assertIsNotNone(kg.get_exact("host:new", "p", "o"))
            kg.close()


class CloseTests(unittest.TestCase):
    def test_close_closes_read_conns_and_reads_degrade(self):
        with tempfile.TemporaryDirectory() as td:
            kg = _kg(Path(td))
            f = kg.assert_fact("h", "p", "o")
            kg.query(subject="h")  # materialize this thread's read conn
            self.assertEqual(len(kg._read_conns), 1)
            kg.close()
            kg.close()  # idempotent
            self.assertEqual(kg._read_conns, [])
            self.assertIsNone(kg.get(f.id))  # contract: None once closed


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
