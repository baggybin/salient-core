"""T3.1 spine (PR6) — build_reconstruction stitches a turn's chain and enforces
the anti-green-paint cross-check (mirror ⊆ scope.db by correlation_id)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from salient_core.bus import ContextStore
from salient_core.coord.reconstruct import build_reconstruction
from salient_core.policy.scope import ScopeStore


def _seed_scope_deny(
    scope: ScopeStore,
    cid: str,
    *,
    tool: str = "curl",
    agent: str = "scout",
    decision_id: str | None = None,
) -> None:
    assert scope._conn is not None
    scope._conn.execute(
        "INSERT INTO scope_decisions "
        "(ts, engagement_id, agent, tool, args_json, targets_json, verdict, reason, "
        "correlation_id, decision_id) VALUES (?, ?, ?, ?, ?, ?, 'deny', ?, ?, ?)",
        (1.0, "op", agent, tool, "{}", "[]", "out of scope", cid, decision_id),
    )
    scope._conn.commit()


class ReconstructTests(unittest.TestCase):
    def _stores(self, td: str):
        ctx = ContextStore(Path(td) / "state.db", engagement_id="op")
        scope = ScopeStore(Path(td) / "scope.db", "op")
        return ctx, scope

    def test_full_chain_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:1"
            try:
                ctx.record_job(
                    agent="scout",
                    job_id=1,
                    prompt="do work",
                    submitted_at=1.0,
                    started_at=1.0,
                    finished_at=5.0,
                    result="done",
                    error=None,
                    correlation_id=cid,
                    assembled_prompt_sha="abc",
                )
                ctx.record_question(
                    qid=1,
                    agent="scout",
                    text="proceed?",
                    job_id=1,
                    kind="operator",
                    asked_at=2.0,
                    correlation_id=cid,
                )
                ctx.record_bypass("scout", "sub", "delegation", "snip", "all", correlation_id=cid)
                ctx.record_audit_mirror(
                    op="scope_deny",
                    agent="scout",
                    correlation_id=cid,
                    tool="curl",
                    reason="oos",
                    tool_use_id="t1",
                )
                ctx.record_audit_mirror(
                    op="safeguard_block",
                    agent="scout",
                    correlation_id=cid,
                    tool="nmap",
                    tool_use_id="t2",
                )
                ctx.record_remote_call(
                    correlation_id=cid,
                    agent="remote_box",
                    session_id="s1",
                    msg_id="m1",
                    tool="remote.read_file",
                    outcome="result",
                )
                ctx.record_usage(
                    agent="scout",
                    model="m",
                    correlation_id=cid,
                    input_tokens=300,
                    output_tokens=100,
                    cost_usd=0.05,
                )
                _seed_scope_deny(scope, cid)  # authoritative twin of the mirror scope_deny

                r = build_reconstruction(ctx, scope, cid)
                self.assertTrue(r["found"])
                self.assertTrue(r["complete"], msg=f"should be complete: {r['scope_gaps']}")
                self.assertEqual(r["coverage"], "scope+safeguard")
                self.assertEqual(len(r["jobs"]), 1)
                self.assertEqual(r["jobs"][0]["assembled_prompt_sha"], "abc")
                self.assertEqual(len(r["questions"]), 1)
                self.assertEqual(len(r["bypasses"]), 1)
                self.assertEqual(len(r["remote_calls"]), 1)
                self.assertEqual(r["usage_totals"]["input_tokens"], 300)
                self.assertAlmostEqual(r["usage_totals"]["cost_usd"], 0.05)
                # chain is time-ordered and covers every category
                kinds = [e["kind"] for e in r["chain"]]
                self.assertEqual(kinds[0], "job")
                self.assertIn("scope_deny", kinds)
                self.assertIn("safeguard_block", kinds)
                self.assertIn("remote_call", kinds)
                self.assertEqual([e["ts"] for e in r["chain"]], sorted(e["ts"] for e in r["chain"]))
            finally:
                ctx.close()
                scope.close()

    def test_dropped_mirror_deny_flags_incomplete_and_surfaces_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:2"
            try:
                # scope.db has a deny, but the best-effort mirror row was dropped.
                _seed_scope_deny(scope, cid, tool="wget")
                r = build_reconstruction(ctx, scope, cid)
                self.assertFalse(r["complete"], "a scope.db deny with no mirror ⇒ incomplete")
                self.assertEqual(len(r["scope_gaps"]), 1)
                self.assertEqual(r["scope_gaps"][0]["mirror"], "missing")
                self.assertEqual(r["scope_gaps"][0]["source"], "authoritative")
                # the gap is surfaced in the chain, not silently omitted
                gap = next(e for e in r["chain"] if "mirror=missing" in e["summary"])
                self.assertEqual(gap["kind"], "scope_deny")
            finally:
                ctx.close()
                scope.close()

    def test_compensating_drop_and_orphan_do_not_read_complete(self) -> None:
        """Equal counts are not equal contents. A count-based cross-check saw
        1 authoritative and 1 mirror row and called this chain clean; the
        multiset difference sees they describe DIFFERENT denies."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:3"
            try:
                # authoritative deny on wget; the mirror instead holds a row for
                # curl (the wget mirror write was dropped, and a curl row exists
                # with no authoritative backing). Cardinality: 1 == 1.
                _seed_scope_deny(scope, cid, tool="wget")
                ctx.record_audit_mirror(
                    op="scope_deny",
                    agent="scout",
                    correlation_id=cid,
                    tool="curl",
                    reason="oos",
                    tool_use_id="t9",
                )

                r = build_reconstruction(ctx, scope, cid)

                self.assertFalse(
                    r["complete"],
                    msg=f"equal counts must not imply equal contents: {r}",
                )
                self.assertEqual(len(r["scope_gaps"]), 1)
                self.assertEqual(r["scope_gaps"][0]["tool"], "wget")
                self.assertEqual(r["orphan_mirror_denies"], 1)
            finally:
                ctx.close()
                scope.close()

    def test_strong_id_join_catches_same_key_drop_and_orphan(self) -> None:
        """T3.1 H1 — the structural check. Two denies on the SAME (agent, tool)
        in one turn: call t1's mirror write was dropped (authoritative only), and
        call t2 was a fail-closed audit-persist deny (mirror only, no scope.db
        row). Under the weak (agent, tool) multiset this cancels — 1 == 1 — and
        reads COMPLETE (the blind spot). With decision_id on both sides it matches
        row-for-row, so t1 is `missing` and t2 is `orphan` ⇒ INCOMPLETE.
        """
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:7"
            try:
                # t1: authoritative deny WITH a decision_id; its mirror was dropped.
                _seed_scope_deny(scope, cid, tool="curl", decision_id="t1")
                # t2: mirror-only deny (fail-closed → no authoritative row).
                ctx.record_audit_mirror(
                    op="scope_deny",
                    agent="scout",
                    correlation_id=cid,
                    tool="curl",
                    reason="oos",
                    tool_use_id="t2",
                )

                r = build_reconstruction(ctx, scope, cid)

                self.assertEqual(r["cross_check"], "decision_id")
                self.assertFalse(r["complete"], f"same-key masking must not read complete: {r}")
                self.assertEqual(r["orphan_mirror_denies"], 1)
                self.assertEqual(len(r["scope_gaps"]), 1)
                self.assertEqual(r["scope_gaps"][0]["tool"], "curl")
                self.assertEqual(r["scope_gaps"][0]["decision_id"], "t1")
            finally:
                ctx.close()
                scope.close()

    def test_strong_id_join_clean_chain_is_complete_and_discloses_tier(self) -> None:
        """An id-matched deny (decision_id == the mirror's tool_use_id) is a clean
        structural chain: COMPLETE, disclosed as cross_check 'decision_id'."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:8"
            try:
                _seed_scope_deny(scope, cid, tool="curl", decision_id="tu1")
                ctx.record_audit_mirror(
                    op="scope_deny",
                    agent="scout",
                    correlation_id=cid,
                    tool="curl",
                    reason="oos",
                    tool_use_id="tu1",
                )

                r = build_reconstruction(ctx, scope, cid)

                self.assertTrue(r["found"])
                self.assertTrue(r["complete"], f"id-matched deny should be complete: {r}")
                self.assertEqual(r["cross_check"], "decision_id")
                self.assertEqual(r["scope_gaps"], [])
                self.assertEqual(r["orphan_mirror_denies"], 0)
            finally:
                ctx.close()
                scope.close()

    def test_legacy_null_decision_id_falls_back_to_weak_tier(self) -> None:
        """A pre-migration deny (NULL decision_id) cannot be id-joined, so the
        chain uses the (agent, tool) multiset and DISCLOSES the weaker tier —
        never silently presenting the weak check as the strong one."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:9"
            try:
                _seed_scope_deny(scope, cid, tool="curl")  # no decision_id
                ctx.record_audit_mirror(
                    op="scope_deny",
                    agent="scout",
                    correlation_id=cid,
                    tool="curl",
                    reason="oos",
                    tool_use_id="tu9",
                )

                r = build_reconstruction(ctx, scope, cid)

                self.assertEqual(r["cross_check"], "agent+tool multiset")
                self.assertTrue(r["complete"])  # matched under the weak key
            finally:
                ctx.close()
                scope.close()

    def test_budget_park_block_surfaces_in_chain_not_as_a_deny_gap(self) -> None:
        """T3.1 H2. A budget-park block is recorded on the same PreToolUse path
        as a deny, but it is NOT a scope deny: it must appear in the chain and
        the structured `blocks` list, and must NOT create a scope_gap / orphan or
        flip `complete` — a block is a real recorded event, not a dropped deny.
        """
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:11"
            try:
                ctx.record_job(
                    agent="scout",
                    job_id=1,
                    prompt="work",
                    submitted_at=1.0,
                    started_at=1.0,
                    finished_at=2.0,
                    result="",
                    error=None,
                    correlation_id=cid,
                )
                ok = ctx.record_block(
                    kind="budget_park",
                    agent="scout",
                    correlation_id=cid,
                    tool="curl",
                    reason="token budget exhausted",
                    tool_use_id="b1",
                )
                self.assertTrue(ok)

                r = build_reconstruction(ctx, scope, cid)

                self.assertTrue(r["found"])
                self.assertTrue(r["complete"], f"a block must not flip complete: {r}")
                self.assertEqual(r["scope_gaps"], [])
                self.assertEqual(r["orphan_mirror_denies"], 0)
                self.assertEqual(len(r["blocks"]), 1)
                self.assertEqual(r["blocks"][0]["op"], "budget_park")
                self.assertEqual(r["block_kinds"], ["budget_park"])
                entry = next(e for e in r["chain"] if e["kind"] == "budget_park")
                self.assertEqual(entry["agent"], "scout")
                self.assertIn("curl", entry["summary"])
            finally:
                ctx.close()
                scope.close()

    def test_record_block_rejects_unknown_kind(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            try:
                with self.assertRaises(ValueError):
                    ctx.record_block(
                        kind="not_a_block", agent="scout", correlation_id="op:scout:1:12"
                    )
            finally:
                ctx.close()
                scope.close()

    def test_killswitch_stop_within_turn_span_joins_into_chain(self) -> None:
        """T3.1 H2. A killswitch STOP is proc-level (agent + time, no request
        id). A STOP dispatched within the turn's [start, end] span, for the
        turn's agent, is time-joined into the chain — so a STOPped turn does not
        reconstruct as a clean chain that just ends. STOPs outside the span, or
        for another agent, are not joined."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:20"
            try:
                ctx.record_job(
                    agent="scout",
                    job_id=1,
                    prompt="work",
                    submitted_at=1.0,
                    started_at=1.0,
                    finished_at=10.0,
                    result="",
                    error=None,
                    correlation_id=cid,
                )
                # within span, right agent → joined
                self.assertTrue(
                    ctx.record_stop_event(
                        agent="scout",
                        dispatched_at=5.0,
                        proven_at=5.2,
                        state="proven_quiescent",
                        reason="operator killswitch",
                    )
                )
                # outside the span → not joined
                ctx.record_stop_event(agent="scout", dispatched_at=99.0, state="force_reaped")
                # right time, wrong agent → not joined
                ctx.record_stop_event(agent="other", dispatched_at=5.0, state="proven_quiescent")

                r = build_reconstruction(ctx, scope, cid)

                self.assertEqual(len(r["stop_events"]), 1)
                self.assertEqual(r["stop_events"][0]["dispatched_at"], 5.0)
                self.assertEqual(r["stop_events"][0]["state"], "proven_quiescent")
                stop = next(e for e in r["chain"] if e["kind"] == "killswitch")
                self.assertEqual(stop["agent"], "scout")
                self.assertIn("STOP", stop["summary"])
                # the STOP slots into the time-ordered chain at ts=5.0
                self.assertEqual(
                    [e["ts"] for e in r["chain"]],
                    sorted(e["ts"] for e in r["chain"]),
                )
            finally:
                ctx.close()
                scope.close()

    def test_stop_join_needs_an_agent_from_a_job(self) -> None:
        """No job ⇒ no agent to key the STOP join on ⇒ no killswitch row, even
        when a STOP exists for that time."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:21"
            try:
                ctx.record_stop_event(agent="scout", dispatched_at=5.0, state="proven_quiescent")
                _seed_scope_deny(scope, cid, tool="curl", decision_id="d1")
                ctx.record_audit_mirror(
                    op="scope_deny",
                    agent="scout",
                    correlation_id=cid,
                    tool="curl",
                    reason="oos",
                    tool_use_id="d1",
                )
                r = build_reconstruction(ctx, scope, cid)
                self.assertEqual(r["stop_events"], [])
                self.assertFalse(any(e["kind"] == "killswitch" for e in r["chain"]))
            finally:
                ctx.close()
                scope.close()

    def test_gap_names_the_unmirrored_deny_not_the_first_row(self) -> None:
        """The surfaced gap must be the deny the mirror actually dropped.
        Slicing `authoritative[:missing]` named whichever row sorted first, so
        an operator chasing a dropped deny was pointed at the wrong tool call."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:4"
            try:
                # Three authoritative denies; the mirror recorded the first two.
                # The dropped one is LAST, so a first-N slice would misreport it.
                for tool in ("curl", "wget", "nc"):
                    _seed_scope_deny(scope, cid, tool=tool)
                ctx.record_audit_mirror(
                    op="scope_deny",
                    agent="scout",
                    correlation_id=cid,
                    tool="curl",
                    reason="oos",
                    tool_use_id="t1",
                )
                ctx.record_audit_mirror(
                    op="scope_deny",
                    agent="scout",
                    correlation_id=cid,
                    tool="wget",
                    reason="oos",
                    tool_use_id="t2",
                )

                r = build_reconstruction(ctx, scope, cid)

                self.assertFalse(r["complete"])
                self.assertEqual(
                    [g["tool"] for g in r["scope_gaps"]],
                    ["nc"],
                    msg="the gap must name the unmirrored deny, not the first row",
                )
                self.assertEqual(r["orphan_mirror_denies"], 0)
                # and it reaches the chain tagged as the authoritative source
                gap = next(e for e in r["chain"] if "mirror=missing" in e["summary"])
                self.assertIn("nc", gap["summary"])
            finally:
                ctx.close()
                scope.close()

    def test_repeated_deny_on_one_tool_counts_per_key(self) -> None:
        """Two denies on the same (agent, tool) with only one mirrored: rows
        sharing a key are interchangeable, so exactly one gap is reported —
        neither swallowed nor double-counted."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:1:5"
            try:
                _seed_scope_deny(scope, cid, tool="curl")
                _seed_scope_deny(scope, cid, tool="curl")
                ctx.record_audit_mirror(
                    op="scope_deny",
                    agent="scout",
                    correlation_id=cid,
                    tool="curl",
                    reason="oos",
                    tool_use_id="t1",
                )

                r = build_reconstruction(ctx, scope, cid)

                self.assertFalse(r["complete"])
                self.assertEqual([g["tool"] for g in r["scope_gaps"]], ["curl"])
                self.assertEqual(r["orphan_mirror_denies"], 0)
            finally:
                ctx.close()
                scope.close()

    def test_cross_check_strength_is_disclosed(self) -> None:
        """`complete` must never be read as a stronger claim than what ran."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            try:
                r = build_reconstruction(ctx, scope, "op:scout:1:6")
                self.assertEqual(r["cross_check"], "agent+tool multiset")
            finally:
                ctx.close()
                scope.close()

    def test_unknown_correlation_is_empty_and_not_complete(self) -> None:
        """An id with no records is ``found=False`` AND ``complete=False``.

        ``complete`` over empty inputs is vacuously "nothing missing, nothing
        orphaned" — but you cannot assert a chain is whole when there is no
        chain. Both renderers gate on ``found`` first, yet the raw dict is the
        contract: a consumer reading ``complete`` directly (a scorer, an RPC
        caller, the mirror cross-check job) must not get a green for a request
        that does not exist.
        """
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            try:
                r = build_reconstruction(ctx, scope, "op:scout:9:9")
                self.assertFalse(r["found"])
                self.assertFalse(r["complete"], "no chain ⇒ not complete")
                self.assertEqual(r["chain"], [])
            finally:
                ctx.close()
                scope.close()

    # ── shadow-mode denies ───────────────────────────────────────────────

    def test_shadowed_deny_is_not_reported_as_a_dropped_record(self) -> None:
        """scope.db logs `verdict='deny'` even when the hook SHADOWS it and lets
        the call run. Counting only enforced denies made that look like the
        mirror had silently dropped a denial — a false alarm on every
        unclassified-builtin call, and backwards: the tool actually RAN."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:2:1"
            try:
                ctx.record_audit_mirror(
                    op="scope_deny_shadowed",
                    agent="scout",
                    correlation_id=cid,
                    tool="Bash",
                    reason="tool 'Bash' has no scope classification",
                    tool_use_id="t1",
                )
                _seed_scope_deny(scope, cid, tool="Bash")

                r = build_reconstruction(ctx, scope, cid)
                self.assertTrue(r["complete"], msg=f"unexpected gap: {r['scope_gaps']}")
                self.assertEqual(r["scope_gaps"], [])
                self.assertEqual(r["shadowed_denies"], 1)
                # The permissive outcome stays visible — it is not laundered
                # into a plain allow.
                entry = next(e for e in r["chain"] if e["kind"] == "scope_deny_shadowed")
                self.assertIn("SHADOWED", entry["summary"])
                self.assertIn("tool RAN", entry["summary"])
            finally:
                ctx.close()
                scope.close()

    def test_shadowed_row_does_not_mask_a_genuinely_dropped_deny(self) -> None:
        """The real alarm must keep working: a shadowed row satisfies only its
        OWN authoritative deny, not an unrelated enforced one."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "op:scout:2:2"
            try:
                ctx.record_audit_mirror(
                    op="scope_deny_shadowed",
                    agent="scout",
                    correlation_id=cid,
                    tool="Bash",
                    reason="unclassified",
                    tool_use_id="t1",
                )
                _seed_scope_deny(scope, cid, tool="Bash")
                _seed_scope_deny(scope, cid, tool="curl")  # enforced, never mirrored

                r = build_reconstruction(ctx, scope, cid)
                self.assertFalse(r["complete"])
                self.assertEqual([g["tool"] for g in r["scope_gaps"]], ["curl"])
            finally:
                ctx.close()
                scope.close()

    # ── legacy (pre-fix) correlation ids ─────────────────────────────────

    def test_legacy_id_is_flagged_ambiguous_and_never_complete(self) -> None:
        """A 3-part id was minted from a process-local epoch, so a restart
        re-issued it: one id can name several unrelated turns. Integrity is
        unknowable when the KEY is ambiguous, so `complete` must not be True
        however clean the cross-check looks."""
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "20260724T190327:4:1"
            try:
                # Two different agents' jobs under the ONE id — exactly what was
                # found on the live engagement.
                ctx.record_job(
                    agent="scanner",
                    job_id=1,
                    prompt="scan",
                    submitted_at=1.0,
                    started_at=1.0,
                    finished_at=2.0,
                    result="",
                    error=None,
                    correlation_id=cid,
                )
                ctx.record_job(
                    agent="nikto",
                    job_id=2,
                    prompt="probe",
                    submitted_at=3.0,
                    started_at=3.0,
                    finished_at=4.0,
                    result="",
                    error=None,
                    correlation_id=cid,
                )

                r = build_reconstruction(ctx, scope, cid)
                self.assertTrue(r["found"])
                self.assertTrue(r["ambiguous"])
                self.assertTrue(r["legacy_id"])
                self.assertFalse(r["complete"])
                # It still returns the rows — the operator can inspect them,
                # they just aren't presented as one verified chain.
                self.assertEqual(len(r["jobs"]), 2)
            finally:
                ctx.close()
                scope.close()

    def test_current_id_is_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctx, scope = self._stores(td)
            cid = "20260724T190327:red_lead:3:1"
            try:
                # A current 4-part id (engagement:agent:epoch:seq) with a clean
                # chain: not ambiguous, and complete once a record exists.
                ctx.record_job(
                    agent="red_lead",
                    job_id=1,
                    prompt="recon",
                    submitted_at=1.0,
                    started_at=1.0,
                    finished_at=2.0,
                    result="",
                    error=None,
                    correlation_id=cid,
                )
                r = build_reconstruction(ctx, scope, cid)
                self.assertTrue(r["found"])
                self.assertFalse(r["ambiguous"])
                self.assertTrue(r["complete"])
            finally:
                ctx.close()
                scope.close()


if __name__ == "__main__":
    unittest.main()
