"""A failed ContextStore commit must roll back, so durable state never diverges
from the in-memory cache (kernel-invariant #2).

sqlite3's default isolation opens an implicit transaction on the first write. If
`commit()` fails and the store does NOT roll back, the pending statement stays in
the connection and a later *unrelated* `commit()` flushes it — after a restart the
rejected value appears durable while the live cache (updated only after a clean
commit) never saw it. The `_txn` helper rolls back on any failure so the pending
write is discarded.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from salient_core.bus import ContextStore


class _FlakyConn(sqlite3.Connection):
    """A connection whose commit() can be made to fail on demand, to simulate a
    commit-time failure (SQLITE_BUSY / disk I/O) without failing the execute."""

    fail_commit = False

    def commit(self) -> None:
        if self.fail_commit:
            raise sqlite3.OperationalError("simulated commit failure")
        super().commit()


class _DoublyFlakyConn(sqlite3.Connection):
    """A connection whose commit() AND rollback() both fail, to simulate the
    worst case where the transaction cannot be cleaned up on the connection."""

    fail = False

    def commit(self) -> None:
        if self.fail:
            raise sqlite3.OperationalError("simulated commit failure")
        super().commit()

    def rollback(self) -> None:
        if self.fail:
            raise sqlite3.OperationalError("simulated rollback failure")
        super().rollback()


class FailedCommitRollbackTests(unittest.TestCase):
    def test_failed_commit_rolls_back_and_never_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ctx.db"
            store = ContextStore(path, events_cap_per_agent=0)
            try:
                store.write("a", "k", "v1")

                # Swap in a connection whose commit() we can fail on demand.
                flaky = sqlite3.connect(str(path), factory=_FlakyConn, check_same_thread=False)
                store._conn = flaky
                flaky.fail_commit = True

                # The update must raise and leave the store consistent.
                with self.assertRaises(sqlite3.OperationalError):
                    store.write("a", "k", "v2")

                # Cache untouched, and the transaction was rolled back — no
                # pending statement for a later commit to flush.
                self.assertEqual(store.read("a", "k"), "v1")
                self.assertFalse(flaky.in_transaction)

                # A later UNRELATED successful write must not resurrect v2.
                flaky.fail_commit = False
                store.write("a", "k2", "other")
            finally:
                store.close()

            # Reopen from disk: the failed value must never have become durable.
            store2 = ContextStore(path, events_cap_per_agent=0)
            try:
                self.assertEqual(store2.read("a", "k"), "v1")
                self.assertEqual(store2.read("a", "k2"), "other")
            finally:
                store2.close()

    def test_failed_delete_commit_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ctx.db"
            store = ContextStore(path, events_cap_per_agent=0)
            try:
                store.write("a", "k", "v1")

                flaky = sqlite3.connect(str(path), factory=_FlakyConn, check_same_thread=False)
                store._conn = flaky
                flaky.fail_commit = True

                with self.assertRaises(sqlite3.OperationalError):
                    store.delete("a", "k")

                # Cache still has the value; the delete didn't half-apply.
                self.assertEqual(store.read("a", "k"), "v1")
                self.assertFalse(flaky.in_transaction)

                flaky.fail_commit = False
                store.write("a", "k2", "other")  # unrelated later commit
            finally:
                store.close()

            store2 = ContextStore(path, events_cap_per_agent=0)
            try:
                # The rejected delete never became durable.
                self.assertEqual(store2.read("a", "k"), "v1")
            finally:
                store2.close()

    def test_rollback_failure_invalidates_connection(self) -> None:
        # If rollback ALSO fails, the connection may still hold the pending
        # statement — reusing it would let a later commit() flush the rejected
        # write (the original bug). The store must drop the connection instead.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ctx.db"
            store = ContextStore(path, events_cap_per_agent=0)
            try:
                store.write("a", "k", "v1")

                broken = sqlite3.connect(
                    str(path), factory=_DoublyFlakyConn, check_same_thread=False
                )
                store._conn = broken
                broken.fail = True

                with self.assertRaises(sqlite3.OperationalError):
                    store.write("a", "k", "v2")

                # Connection dropped => no later commit() can flush the dirty
                # transaction; the store degrades to cache-only.
                self.assertIsNone(store._conn)
                self.assertEqual(store.read("a", "k"), "v1")
                # A subsequent write is cache-only and must not raise.
                store.write("a", "k3", "cache-only")
                self.assertEqual(store.read("a", "k3"), "cache-only")
            finally:
                store.close()

            # v2 never became durable (commit failed, conn closed → auto-rollback).
            store2 = ContextStore(path, events_cap_per_agent=0)
            try:
                self.assertEqual(store2.read("a", "k"), "v1")
                # The cache-only write was never persisted (no connection).
                self.assertIsNone(store2.read("a", "k3"))
            finally:
                store2.close()


if __name__ == "__main__":
    unittest.main()
