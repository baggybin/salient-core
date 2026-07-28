"""T3.1 spine — remote_calls records each forwarded remote.* call by correlation.

Pins the ContextStore side of PR3: a remote call round-trips (correlation_id +
wire msg_id + session + outcome), reads back oldest→newest, and a write failure
signals False without raising.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from salient_core.bus import ContextStore


class RemoteCallsTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db", engagement_id="op")
            try:
                cid = "op:2:3"
                self.assertTrue(
                    store.record_remote_call(
                        correlation_id=cid,
                        agent="remote_box",
                        session_id="s1",
                        msg_id="m1",
                        tool="remote.read_file",
                        outcome="result",
                    )
                )
                self.assertTrue(
                    store.record_remote_call(
                        correlation_id=cid,
                        agent="remote_box",
                        session_id="s1",
                        msg_id="m2",
                        tool="remote.run_command",
                        outcome="denied",
                        detail="tool_not_allowed: not in grant",
                    )
                )
                rows = store.load_remote_calls(cid)
                self.assertEqual([r["msg_id"] for r in rows], ["m1", "m2"])
                self.assertEqual(rows[0]["tool"], "remote.read_file")
                self.assertEqual(rows[1]["outcome"], "denied")
                self.assertEqual(rows[0]["session_id"], "s1")
            finally:
                store.close()

    def test_detail_is_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db")
            try:
                store.record_remote_call(
                    correlation_id="c",
                    agent="a",
                    session_id="s",
                    msg_id="m",
                    tool="remote.run_command",
                    outcome="error",
                    detail="x" * 2000,
                )
                self.assertLessEqual(len(store.load_remote_calls("c")[0]["detail"]), 500)
            finally:
                store.close()

    def test_write_failure_signals_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db")
            try:
                with store._lock:
                    store._conn.execute("DROP TABLE remote_calls")
                    store._conn.commit()
                self.assertFalse(
                    store.record_remote_call(
                        correlation_id="c",
                        agent="a",
                        session_id="s",
                        msg_id="m",
                        tool="remote.read_file",
                    )
                )
                self.assertTrue(store.degraded)
            finally:
                store.close()

    def test_no_db_is_not_a_failure(self) -> None:
        store = ContextStore(None)
        self.assertTrue(
            store.record_remote_call(
                correlation_id="c", agent="a", session_id="s", msg_id="m", tool="remote.read_file"
            )
        )


if __name__ == "__main__":
    unittest.main()
