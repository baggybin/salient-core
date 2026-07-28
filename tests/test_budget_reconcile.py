"""Pins for `budget_reconcile` — the cross-check between the two accountings.

The budget ledger (enforcement) and `usage_ledger` (audit) record the same turns
by different routes and share no key, so nothing used to notice when they
disagreed. A live engagement carried a 662,254-token divergence — spend no
ceiling would ever have seen — until it was summed by hand.
"""

from __future__ import annotations

from pathlib import Path

from salient_core.bus import ContextStore
from salient_core.daemon._runner_factory import _RunnerFactoryMixin


class _Daemon(_RunnerFactoryMixin):
    def __init__(self, context: ContextStore | None) -> None:
        self.context = context
        self.runners: dict[str, object] = {}


def _record_turn(ctx: ContextStore, agent: str, cid: str, tokens: int) -> None:
    """One audit row worth `tokens`, split across the four charged categories."""
    q = tokens // 4
    ctx.record_usage(
        agent=agent,
        model="m",
        correlation_id=cid,
        input_tokens=q,
        output_tokens=q,
        cache_create_tokens=q,
        cache_read_tokens=tokens - 3 * q,
    )


def test_agreeing_ledgers_report_nothing(tmp_path: Path) -> None:
    ctx = ContextStore(tmp_path / "state.db", engagement_id="op")
    d = _Daemon(ctx)
    try:
        _record_turn(ctx, "sol", "op:sol:1:1", 1000)
        d._budget_ledger_get().charge("sol", 1, 1, 1000)
        assert d.budget_reconcile() == []
    finally:
        ctx.close()


def test_undercharge_is_reported_with_a_negative_delta(tmp_path: Path) -> None:
    ctx = ContextStore(tmp_path / "state.db", engagement_id="op")
    d = _Daemon(ctx)
    try:
        # Two turns really happened; only the first was charged — precisely the
        # live failure, where a restart re-issued the ledger key and the second
        # charge was swallowed as a duplicate.
        _record_turn(ctx, "red_lead", "op:red_lead:3:1", 2000)
        _record_turn(ctx, "red_lead", "op:red_lead:3:2", 3000)
        d._budget_ledger_get().charge("red_lead", 3, 1, 2000)

        [row] = d.budget_reconcile()
        assert row["agent"] == "red_lead"
        assert row["charged_tokens"] == 2000
        assert row["usage_tokens"] == 5000
        # Negative: the ledger charged LESS than was spent — the direction that
        # means a ceiling could be walked past.
        assert row["delta_tokens"] == -3000
        assert row["usage_turns"] == 2
    finally:
        ctx.close()


def test_only_diverging_agents_are_reported(tmp_path: Path) -> None:
    ctx = ContextStore(tmp_path / "state.db", engagement_id="op")
    d = _Daemon(ctx)
    try:
        _record_turn(ctx, "ok_agent", "op:ok_agent:1:1", 800)
        d._budget_ledger_get().charge("ok_agent", 1, 1, 800)
        _record_turn(ctx, "bad_agent", "op:bad_agent:1:1", 900)

        assert [r["agent"] for r in d.budget_reconcile()] == ["bad_agent"]
    finally:
        ctx.close()


def test_no_context_or_no_rows_is_quiet(tmp_path: Path) -> None:
    # No context at all (unit-test daemon) and an empty ledger both report
    # nothing rather than raising into the boot path.
    assert _Daemon(None).budget_reconcile() == []

    ctx = ContextStore(tmp_path / "state.db", engagement_id="op")
    try:
        assert _Daemon(ctx).budget_reconcile() == []
    finally:
        ctx.close()


def test_read_failure_does_not_raise_into_boot(tmp_path: Path) -> None:
    class _Broken(ContextStore):
        def usage_totals_by_agent(self) -> dict:
            raise RuntimeError("db gone")

    ctx = _Broken(tmp_path / "state.db", engagement_id="op")
    try:
        assert _Daemon(ctx).budget_reconcile() == []
    finally:
        ctx.close()
