"""T3.1 spine — the joinable audit_mirror (scope_deny + safeguard_block).

Pins Stage A of PR2: the mirror table round-trips, `seq` is a per-correlation
monotonic tiebreaker, the write is idempotent on `tool_use_id` (a stray second
call can't double-count), and record_audit_mirror signals success so a caller
can surface a dropped safeguard_block (its only durable home) instead of
swallowing it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from salient_core.bus import ContextStore


class AuditMirrorTests(unittest.TestCase):
    def test_round_trip_and_per_correlation_seq(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db", engagement_id="op-x")
            try:
                cid = "op-x:1:4"
                self.assertTrue(
                    store.record_audit_mirror(
                        op="scope_deny",
                        agent="scout",
                        correlation_id=cid,
                        tool="curl",
                        target="10.0.0.9",
                        reason="out of scope",
                        tool_use_id="tu-1",
                    )
                )
                self.assertTrue(
                    store.record_audit_mirror(
                        op="safeguard_block",
                        agent="scout",
                        correlation_id=cid,
                        tool="nmap",
                        reason="loud in stealth",
                        tool_use_id="tu-2",
                    )
                )
                # a different turn
                self.assertTrue(
                    store.record_audit_mirror(
                        op="scope_deny",
                        agent="scout",
                        correlation_id="op-x:1:5",
                        tool="ssh",
                        tool_use_id="tu-3",
                    )
                )

                chain = store.load_audit_mirror(cid)
                self.assertEqual([r["seq"] for r in chain], [1, 2])
                self.assertEqual([r["op"] for r in chain], ["scope_deny", "safeguard_block"])
                self.assertEqual(chain[0]["target"], "10.0.0.9")
                # seq is per-correlation: the other turn restarts at 1
                other = store.load_audit_mirror("op-x:1:5")
                self.assertEqual([r["seq"] for r in other], [1])
            finally:
                store.close()

    def test_idempotent_on_tool_use_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db")
            try:
                cid = "e:1:1"
                store.record_audit_mirror(
                    op="scope_deny", agent="a", correlation_id=cid, tool_use_id="dup"
                )
                # replayed PreToolUse for the same tool call → must not double-count
                store.record_audit_mirror(
                    op="scope_deny", agent="a", correlation_id=cid, tool_use_id="dup"
                )
                self.assertEqual(len(store.load_audit_mirror(cid)), 1)
            finally:
                store.close()

    def test_null_tool_use_ids_are_not_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db")
            try:
                cid = "e:1:2"
                store.record_audit_mirror(op="safeguard_block", agent="a", correlation_id=cid)
                store.record_audit_mirror(op="safeguard_block", agent="a", correlation_id=cid)
                # NULL tool_use_id ⇒ distinct under the partial unique index
                self.assertEqual(len(store.load_audit_mirror(cid)), 2)
            finally:
                store.close()

    def test_write_failure_signals_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db")
            try:
                # Corrupt the table so the INSERT fails → the caller must learn
                # the safeguard_block was dropped (record returns False).
                with store._lock:
                    store._conn.execute("DROP TABLE audit_mirror")
                    store._conn.commit()
                ok = store.record_audit_mirror(
                    op="safeguard_block", agent="a", correlation_id="e:1:3"
                )
                self.assertFalse(ok)
                self.assertTrue(store.degraded)
            finally:
                store.close()

    def test_no_db_is_not_a_failure(self) -> None:
        store = ContextStore(None)  # in-memory only, no durable store
        self.assertTrue(
            store.record_audit_mirror(op="scope_deny", agent="a", correlation_id="e:1:4")
        )

    def test_audit_summary_counts_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db", engagement_id="op")
            try:
                store.record_audit_mirror(
                    op="scope_deny", agent="a", correlation_id="op:1:1", tool_use_id="s1"
                )
                store.record_audit_mirror(
                    op="safeguard_block", agent="a", correlation_id="op:1:1", tool_use_id="s2"
                )
                store.record_audit_mirror(
                    op="safeguard_block", agent="a", correlation_id="op:1:2", tool_use_id="s3"
                )
                store.record_bypass("a", "b", "delegation", "x", "all", correlation_id="op:1:1")
                store.record_remote_call(
                    correlation_id="op:1:1",
                    agent="a",
                    session_id="s",
                    msg_id="m",
                    tool="remote.read_file",
                )
                summ = store.audit_summary()
                self.assertEqual(summ["scope_denies"], 1)
                self.assertEqual(summ["safeguard_blocks"], 2)
                self.assertEqual(summ["trust_bypasses"], 1)
                self.assertEqual(summ["remote_calls"], 1)
                self.assertEqual(summ["audit_failures"], 0)
                # a swallowed audit write bumps audit_failures
                with store._lock:
                    store._conn.execute("DROP TABLE usage_ledger")
                    store._conn.commit()
                store.record_usage(agent="a", model=None, correlation_id="op:1:1")
                self.assertGreaterEqual(store.audit_summary()["audit_failures"], 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
