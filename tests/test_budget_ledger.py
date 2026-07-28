"""Pins for the pure token-budget primitives (salient_core.daemon._budget).

These lock the anti-laundering + idempotency + turn-boundary semantics the
budget control rung depends on. The stateful runner/daemon wiring is pinned
separately; here we prove the arithmetic core in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from salient_core.daemon._budget import (
    BudgetAction,
    BudgetLedger,
    evaluate_budget,
    turn_tokens,
)


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    cost_usd: float | None = None


def test_turn_tokens_counts_all_categories():
    u = _Usage(input_tokens=10, output_tokens=5, cache_read_tokens=3, cache_create_tokens=2)
    assert turn_tokens(u) == 20


def test_ledger_charge_is_idempotent():
    led = BudgetLedger()
    assert led.charge("sol", epoch=1, turn_seq=1, tokens=100) is True
    # same (agent, epoch, turn_seq) is a no-op, never double-charges
    assert led.charge("sol", epoch=1, turn_seq=1, tokens=100) is False
    assert led.spent_for_agent("sol") == 100


def test_reset_does_not_launder_spend():
    """A runner reset bumps the epoch; old-epoch entries must survive so the
    new incarnation inherits the spend (the anti-laundering property)."""
    led = BudgetLedger()
    led.charge("sol", epoch=1, turn_seq=1, tokens=400)
    led.charge("sol", epoch=1, turn_seq=2, tokens=400)
    # ... reset happens: same agent, fresh epoch 2 ...
    led.charge("sol", epoch=2, turn_seq=1, tokens=50)
    assert led.spent_for_agent("sol") == 850  # 800 old + 50 new, NOT 50


def test_pool_spend_sums_across_agents():
    led = BudgetLedger()
    led.charge("sol", epoch=1, turn_seq=1, tokens=300)
    led.charge("luna", epoch=1, turn_seq=1, tokens=200)
    assert led.spent_total() == 500
    assert led.spent_for_agents({"sol", "luna"}) == 500
    assert led.spent_for_agents({"sol"}) == 300


def test_snapshot_round_trip_survives_restart():
    led = BudgetLedger()
    led.charge("sol", epoch=1, turn_seq=1, tokens=123)
    led.charge("luna", epoch=3, turn_seq=7, tokens=456)
    restored = BudgetLedger.from_snapshot(led.snapshot())
    assert restored.spent_for_agent("sol") == 123
    assert restored.spent_for_agent("luna") == 456
    assert restored.spent_total() == 579


def test_from_snapshot_skips_bad_rows_without_zeroing():
    good = {"agent": "sol", "epoch": 1, "turn_seq": 1, "tokens": 99}
    bad = {"agent": "luna"}  # missing keys
    led = BudgetLedger.from_snapshot([good, bad])
    assert led.spent_for_agent("sol") == 99  # good row preserved


def test_evaluate_none_ceiling_is_observational():
    v = evaluate_budget(spent=10_000, ceiling=None, warn_frac=0.8, on_exhaustion="pause")
    assert v.action is BudgetAction.OK
    assert v.enforced is False


def test_evaluate_warn_then_over():
    ok = evaluate_budget(spent=100, ceiling=1000, warn_frac=0.8, on_exhaustion="pause")
    assert ok.action is BudgetAction.OK

    warn = evaluate_budget(spent=800, ceiling=1000, warn_frac=0.8, on_exhaustion="pause")
    assert warn.action is BudgetAction.WARN

    over = evaluate_budget(spent=1000, ceiling=1000, warn_frac=0.8, on_exhaustion="pause")
    assert over.action is BudgetAction.OVER
    assert over.enforced is True


def test_evaluate_warn_only_action_is_not_enforced():
    over = evaluate_budget(spent=2000, ceiling=1000, warn_frac=0.8, on_exhaustion="warn")
    assert over.action is BudgetAction.OVER
    assert over.enforced is False  # monitor-only: over the ceiling but never stops


def test_evaluate_bad_warn_frac_defaults_to_point_eight():
    v = evaluate_budget(spent=800, ceiling=1000, warn_frac=5.0, on_exhaustion="pause")
    assert v.action is BudgetAction.WARN  # 5.0 clamped to 0.8 → 800 >= 800
