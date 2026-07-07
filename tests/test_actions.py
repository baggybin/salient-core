"""Unit tests for salient/actions.py — the per-engagement action ledger.

Run: `python -m unittest tests.test_actions` (stdlib only).

Coverage:
  - canonical_args: dict-order-independent hash + canonical JSON
  - ActionLedger: record_start / record_finish / query / recent_for_targets
  - target_key_for_call: extractor-spec lookup for common tool shapes
  - extract_target_keys_from_text: IP / host / URL recognition for
    prompt-injection target discovery
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from salient_core.memory import actions as A


class TestCanonicalArgs(unittest.TestCase):
    def test_hash_is_order_independent(self):
        _, h1 = A.canonical_args({"target": "10.0.0.5", "ports": "1-1024"})
        _, h2 = A.canonical_args({"ports": "1-1024", "target": "10.0.0.5"})
        self.assertEqual(h1, h2, "dict order must not affect args_hash")

    def test_hash_is_12_chars(self):
        _, h = A.canonical_args({"x": 1})
        self.assertEqual(len(h), 12)

    def test_hash_differs_on_value_change(self):
        _, h1 = A.canonical_args({"target": "10.0.0.5"})
        _, h2 = A.canonical_args({"target": "10.0.0.6"})
        self.assertNotEqual(h1, h2)

    def test_canonical_json_is_sorted(self):
        canon, _ = A.canonical_args({"b": 2, "a": 1})
        # sorted keys → 'a' appears before 'b' in the string
        self.assertLess(canon.index('"a"'), canon.index('"b"'))

    def test_unserializable_input_doesnt_crash(self):
        # Sets aren't JSON-serializable — exercise the fallback path.
        canon, h = A.canonical_args({"thing": {1, 2, 3}})
        self.assertIsInstance(canon, str)
        self.assertEqual(len(h), 12)


class TestLedgerRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = A.ActionLedger(Path(self.tmp.name) / "engagement.db")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_start_finish_query_roundtrip(self):
        aid = self.ledger.record_start(
            agent="scanner",
            job_id=7,
            tool="nmap_scan",
            args={"target": "10.0.0.5"},
            target_key="host:10.0.0.5",
        )
        self.assertGreater(aid, 0)
        self.ledger.record_finish(aid, outcome="ok", summary="open: 22,80,443")

        rows = self.ledger.query(target="10.0.0.5")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.agent, "scanner")
        self.assertEqual(r.job_id, 7)
        self.assertEqual(r.tool, "nmap_scan")
        self.assertEqual(r.target_key, "host:10.0.0.5")
        self.assertEqual(r.outcome, "ok")
        self.assertEqual(r.summary, "open: 22,80,443")
        self.assertIsNotNone(r.finished_ts)

    def test_inflight_row_has_no_outcome(self):
        self.ledger.record_start(
            agent="scanner",
            job_id=1,
            tool="nmap_scan",
            args={"target": "10.0.0.5"},
            target_key="host:10.0.0.5",
        )
        rows = self.ledger.query(target="10.0.0.5")
        self.assertEqual(rows[0].outcome, None)
        self.assertEqual(rows[0].finished_ts, None)

    def test_query_filters(self):
        a1 = self.ledger.record_start(
            agent="scanner",
            job_id=1,
            tool="nmap_scan",
            args={"t": 1},
            target_key="host:10.0.0.5",
        )
        a2 = self.ledger.record_start(
            agent="webapp",
            job_id=1,
            tool="nikto.scan",
            args={"t": 2},
            target_key="url:http://10.0.0.5/",
        )
        self.ledger.record_finish(a1, outcome="ok", summary="s1")
        self.ledger.record_finish(a2, outcome="ok", summary="s2")

        # filter by tool
        rows = self.ledger.query(tool="nmap")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].tool, "nmap_scan")

        # filter by agent
        rows = self.ledger.query(agent="webapp")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].agent, "webapp")

        # filter by target (substring) — '10.0.0.5' matches both
        rows = self.ledger.query(target="10.0.0.5")
        self.assertEqual(len(rows), 2)

        # filter by since_ts in the future → empty
        rows = self.ledger.query(since_ts=time.time() + 3600)
        self.assertEqual(rows, [])

    def test_recent_for_targets_dedupes_and_orders(self):
        # Two actions touching host:10.0.0.5; one touching url:http://x
        a1 = self.ledger.record_start(
            agent="scanner",
            job_id=1,
            tool="nmap_scan",
            args={"t": 1},
            target_key="host:10.0.0.5",
            ts=1000.0,
        )
        a2 = self.ledger.record_start(
            agent="webapp",
            job_id=1,
            tool="gobuster.scan",
            args={"t": 2},
            target_key="host:10.0.0.5",
            ts=2000.0,
        )
        a3 = self.ledger.record_start(
            agent="webapp",
            job_id=2,
            tool="nikto.scan",
            args={"t": 3},
            target_key="url:http://other.example/",
            ts=3000.0,
        )

        rows = self.ledger.recent_for_targets(
            ["host:10.0.0.5"],
            per_target_limit=5,
            overall_limit=10,
        )
        ids = [r.id for r in rows]
        # Newest first within target
        self.assertEqual(ids, [a2, a1])

        # Multi-target union, deduped
        rows = self.ledger.recent_for_targets(
            ["host:10.0.0.5", "url:http://other.example/"],
        )
        ids = [r.id for r in rows]
        # Newest-first overall, no duplicates
        self.assertEqual(ids, [a3, a2, a1])

    def test_get_by_id_roundtrips(self):
        aid = self.ledger.record_start(
            agent="scanner",
            job_id=3,
            tool="nmap_scan",
            args={"target": "10.0.0.5"},
            target_key="host:10.0.0.5",
        )
        got = self.ledger.get(aid)
        self.assertIsNotNone(got)
        self.assertEqual(got.id, aid)
        self.assertEqual(got.agent, "scanner")
        self.assertEqual(got.tool, "nmap_scan")

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.ledger.get(999_999))

    def test_get_after_close_returns_none(self):
        aid = self.ledger.record_start(
            agent="a",
            job_id=1,
            tool="t",
            args={},
            target_key=None,
        )
        self.ledger.close()
        self.assertIsNone(self.ledger.get(aid))

    def test_summary_is_truncated(self):
        aid = self.ledger.record_start(
            agent="a",
            job_id=1,
            tool="t",
            args={},
            target_key=None,
        )
        self.ledger.record_finish(aid, outcome="ok", summary="x" * 1000)
        row = self.ledger.query()[0]
        # 499 + ellipsis
        self.assertLessEqual(len(row.summary), 500)
        self.assertTrue(row.summary.endswith("…"))


class TestTargetKeyForCall(unittest.TestCase):
    def test_ip_tool_yields_target_kind(self):
        # 'ping' uses the generic 'ip_or_host' extractor on field 'target'.
        # scope.TargetKind ∈ {"ip","network","host","url"} — accept any.
        tk = A.target_key_for_call("ping", {"target": "10.0.0.5"})
        self.assertIsNotNone(tk)
        kind = tk.split(":", 1)[0]
        self.assertIn(kind, {"ip", "network", "host"})
        self.assertIn("10.0.0.5", tk)

    def test_url_tool_yields_url_key(self):
        # 'curl' uses the generic 'url_or_host' extractor on field 'target'.
        tk = A.target_key_for_call("curl", {"target": "http://10.0.0.5/admin"})
        self.assertIsNotNone(tk)
        self.assertTrue("10.0.0.5" in tk or "url" in tk)

    def test_unknown_tool_returns_none(self):
        tk = A.target_key_for_call("not_a_tool_we_know", {"target": "10.0.0.5"})
        self.assertIsNone(tk)

    def test_bus_tool_returns_none(self):
        # context_write has no scope target (spec.none)
        tk = A.target_key_for_call("context_write", {"key": "k", "value": "v"})
        self.assertIsNone(tk)

    def test_non_dict_args_returns_none(self):
        self.assertIsNone(A.target_key_for_call("ping", "10.0.0.5"))
        self.assertIsNone(A.target_key_for_call("ping", None))


class TestExtractTargetKeysFromText(unittest.TestCase):
    def test_finds_ipv4(self):
        keys = A.extract_target_keys_from_text("Please scan 10.0.0.5 for open ports")
        self.assertIn("ip:10.0.0.5", keys)

    def test_finds_url_as_host_kind(self):
        # URLs reduce to hostname (matches scope's behavior) — so the
        # ledger lookup converges across textual mentions and tool calls.
        keys = A.extract_target_keys_from_text("Check http://example.com/admin for XSS")
        self.assertIn("host:example.com", keys)

    def test_url_with_ip_host_yields_ip_kind(self):
        # Same convergence with an IP-hosted URL: alignment between
        # nikto.scan target_key and a text mention of the bare IP.
        keys = A.extract_target_keys_from_text("Run nikto on http://10.0.0.5/admin")
        self.assertIn("ip:10.0.0.5", keys)

    def test_finds_fqdn(self):
        keys = A.extract_target_keys_from_text("Enumerate corp.local for shares")
        self.assertIn("host:corp.local", keys)

    def test_caps_result_count(self):
        # 20 hosts mentioned — should cap at 8.
        text = " ".join(f"host{i}.local" for i in range(20))
        keys = A.extract_target_keys_from_text(text)
        self.assertLessEqual(len(keys), 8)

    def test_empty_text_returns_empty(self):
        self.assertEqual(A.extract_target_keys_from_text(""), [])
        self.assertEqual(A.extract_target_keys_from_text(None), [])  # type: ignore[arg-type]

    def test_no_targets_returns_empty(self):
        self.assertEqual(
            A.extract_target_keys_from_text("just a general status update"),
            [],
        )


class TestActionLine(unittest.TestCase):
    def test_to_line_renders_all_fields(self):
        a = A.Action(
            id=1,
            agent="scanner",
            job_id=7,
            tool="nmap_scan",
            args_hash="abcd1234",
            args_json="{}",
            target_key="host:10.0.0.5",
            started_ts=time.time(),
            finished_ts=time.time(),
            outcome="ok",
            summary="open: 22,80,443",
        )
        line = a.to_line()
        self.assertIn("scanner", line)
        self.assertIn("nmap_scan", line)
        self.assertIn("10.0.0.5", line)
        self.assertIn("ok", line)
        self.assertIn("open", line)

    def test_to_line_handles_inflight_row(self):
        a = A.Action(
            id=1,
            agent="scanner",
            job_id=7,
            tool="nmap_scan",
            args_hash="abcd1234",
            args_json="{}",
            target_key=None,
            started_ts=time.time(),
            finished_ts=None,
            outcome=None,
            summary=None,
        )
        line = a.to_line()
        # In-flight marker present, no crashes
        self.assertIn("…", line)
        self.assertIn("-", line)


class TestCountRecent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = A.ActionLedger(Path(self.tmp.name) / "eng.db")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_counts_only_matching_tool_hash(self):
        # Three calls with same args (same hash), one with different args
        args1 = {"target": "10.0.0.5"}
        args2 = {"target": "10.0.0.6"}
        _, h1 = A.canonical_args(args1)
        _, h2 = A.canonical_args(args2)

        for _ in range(3):
            self.ledger.record_start(
                agent="scanner",
                job_id=1,
                tool="nmap_scan",
                args=args1,
                target_key="ip:10.0.0.5",
            )
        self.ledger.record_start(
            agent="scanner",
            job_id=1,
            tool="nmap_scan",
            args=args2,
            target_key="ip:10.0.0.6",
        )

        # since=0 → unbounded lookback
        self.assertEqual(
            self.ledger.count_recent(tool="nmap_scan", args_hash=h1, since_ts=0),
            3,
        )
        self.assertEqual(
            self.ledger.count_recent(tool="nmap_scan", args_hash=h2, since_ts=0),
            1,
        )

    def test_respects_since_ts(self):
        args = {"target": "10.0.0.5"}
        _, h = A.canonical_args(args)
        now = time.time()
        # one old, one new
        self.ledger.record_start(
            agent="a",
            job_id=1,
            tool="t",
            args=args,
            target_key=None,
            ts=now - 3600,
        )
        self.ledger.record_start(
            agent="a",
            job_id=1,
            tool="t",
            args=args,
            target_key=None,
            ts=now - 60,
        )
        # window 30 min → only one
        self.assertEqual(
            self.ledger.count_recent(
                tool="t",
                args_hash=h,
                since_ts=now - 30 * 60,
            ),
            1,
        )
        # window 2 hours → both
        self.assertEqual(
            self.ledger.count_recent(
                tool="t",
                args_hash=h,
                since_ts=now - 7200,
            ),
            2,
        )

    def test_count_zero_for_no_matches(self):
        self.assertEqual(
            self.ledger.count_recent(
                tool="nonexistent",
                args_hash="abc123",
                since_ts=0,
            ),
            0,
        )


class TestDelegationEnvelope(unittest.TestCase):
    """The envelope is the soft-contract block prepended to delegated
    prompts when ask_agent gets max_turns / deliverable."""

    def test_empty_when_no_fields(self):
        from salient_core.bus import _render_delegation_envelope

        self.assertEqual(
            _render_delegation_envelope(
                caller="sherlock",
                max_turns_hint=0,
                deliverable="",
            ),
            "",
        )

    def test_renders_budget_only(self):
        from salient_core.bus import _render_delegation_envelope

        out = _render_delegation_envelope(
            caller="sherlock",
            max_turns_hint=5,
            deliverable="",
        )
        self.assertIn("'sherlock'", out)
        # HARD CEILING wording is the contract — soft "should fit in"
        # framing was retired after observed runaway chains where
        # shadows read the budget as a target instead of a ceiling.
        self.assertIn("HARD CEILING", out)
        self.assertIn("5 turns", out)
        self.assertNotIn("Return:", out)

    def test_renders_deliverable_only(self):
        from salient_core.bus import _render_delegation_envelope

        out = _render_delegation_envelope(
            caller="sherlock",
            max_turns_hint=0,
            deliverable="list of open TCP ports as CSV",
        )
        self.assertIn("Return: list of open TCP ports as CSV", out)
        self.assertNotIn("Budget:", out)

    def test_renders_both(self):
        from salient_core.bus import _render_delegation_envelope

        out = _render_delegation_envelope(
            caller="orchestrator",
            max_turns_hint=3,
            deliverable="a single CVE id or 'none'",
        )
        self.assertIn("Budget:", out)
        self.assertIn("HARD CEILING", out)
        self.assertIn("3 turns", out)
        self.assertIn("Return: a single CVE id or 'none'", out)
        self.assertIn("'orchestrator'", out)

    def test_singular_turn_grammar(self):
        from salient_core.bus import _render_delegation_envelope

        out = _render_delegation_envelope(
            caller="x",
            max_turns_hint=1,
            deliverable="",
        )
        # "1 turn", not "1 turns"
        self.assertIn("1 turn.", out)
        self.assertNotIn("1 turns", out)

    def test_negative_max_turns_treated_as_no_hint(self):
        from salient_core.bus import _render_delegation_envelope

        out = _render_delegation_envelope(
            caller="x",
            max_turns_hint=-1,
            deliverable="",
        )
        self.assertEqual(out, "")


class TestLedgerShutdownRace(unittest.TestCase):
    """close() is idempotent and post-close writes drop instead of touching a
    closed sqlite handle (shutdown-race guard back-ported from salient)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = A.ActionLedger(Path(self.tmp.name) / "eng.db")

    def tearDown(self):
        self.ledger.close()  # must be safe even though the test already closed
        self.tmp.cleanup()

    def test_double_close_is_idempotent(self):
        self.ledger.close()
        self.ledger.close()  # would raise on a bare self._conn.close()

    def test_record_start_after_close_drops(self):
        self.ledger.close()
        aid = self.ledger.record_start(agent="a", job_id=1, tool="t", args={}, target_key=None)
        self.assertEqual(aid, 0)

    def test_record_finish_after_close_drops(self):
        aid = self.ledger.record_start(agent="a", job_id=1, tool="t", args={}, target_key=None)
        self.ledger.close()
        # no raise — write is silently dropped
        self.ledger.record_finish(aid, outcome="ok", summary="x")


if __name__ == "__main__":
    unittest.main()
