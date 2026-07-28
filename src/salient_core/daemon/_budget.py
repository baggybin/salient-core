"""Token-budget enforcement primitives (kernel mechanism).

`budget` is a control-ladder rung: an operator sets a token ceiling per
agent (or a shared per-engagement pool) and the daemon enforces it BELOW
the model — warn, soft-stop (park), or hard-stop (interrupt) — not by
prompt politeness.

This module holds the *pure, testable* mechanism:

  * ``BudgetLedger`` — an append-only spend ledger keyed by
    ``(agent, epoch, turn_seq)``. ``spent`` is always **derived** by
    summing entries, never a mutable counter. Because it is keyed by the
    runner's per-incarnation ``epoch`` and lives on the daemon (not the
    runner), a ``reset`` — which destroys+recreates the runner under a
    fresh epoch — cannot zero it. That is the anti-laundering property:
    the old epoch's entries survive, so an agent that blew its budget
    re-parks immediately on the next incarnation.
  * ``evaluate_budget`` — the pure spend→verdict decision. It reports a
    *level* (ok / warn / over); the runner owns the enforcement *action*
    (park vs interrupt), because the interrupt primitive lives there.

Honest semantics: accounting is exact, but enforcement happens at a TURN
BOUNDARY (usage arrives on ``TurnCompletedEvent``), so up to one completed
turn of overshoot past the ceiling is unavoidable. We do NOT add a grace
buffer — that would silently weaken the configured ceiling.

Dollars are deliberately absent: ``cost_usd`` is notional on Pro/Max plans
and there is no pricing table, so the enforced unit is always tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BudgetAction(StrEnum):
    """Verdict level from :func:`evaluate_budget`.

    Deliberately a *level*, not an enforcement action: the runner maps
    ``OVER`` to soft-stop/hard-stop using its own gate state, since only
    the runner can interrupt the live SDK turn.
    """

    OK = "ok"
    WARN = "warn"
    OVER = "over"


# What the operator wants to happen once the ceiling is crossed. Echoed
# through the verdict so the runner knows what ``OVER`` means without
# re-reading config.
ON_EXHAUSTION_VALUES = ("warn", "pause", "stop")


@dataclass(frozen=True)
class BudgetVerdict:
    """Result of charging one turn against the budget."""

    action: BudgetAction
    # Echo of the configured ``on_exhaustion`` ("warn" | "pause" | "stop")
    # that applies at the ceiling; "" when no ceiling is set.
    on_exhaustion: str = ""
    spent: int = 0
    ceiling: int | None = None
    # "agent" | "engagement" — which scope's ceiling bit first. Empty when OK.
    scope: str = ""
    reason: str = ""

    @property
    def enforced(self) -> bool:
        """True when this verdict should stop the agent (OVER + a stopping
        action). ``on_exhaustion == "warn"`` is monitor-only and never
        enforced."""
        return self.action is BudgetAction.OVER and self.on_exhaustion in ("pause", "stop")


@dataclass
class BudgetLedger:
    """Append-only, idempotent token-spend ledger.

    Keyed by ``(agent, epoch, turn_seq)`` so a duplicate
    ``TurnCompletedEvent`` (event replay, reconnect) never double-charges,
    and a runner ``reset`` (new epoch) adds to — never replaces — prior
    spend.
    """

    _entries: dict[tuple[str, int, int], int] = field(default_factory=dict)

    def charge(self, agent: str, epoch: int, turn_seq: int, tokens: int) -> bool:
        """Record ``tokens`` for one turn. Returns True if newly recorded,
        False if this exact ``(agent, epoch, turn_seq)`` was already charged
        (idempotent no-op — never double-counts)."""
        key = (str(agent), int(epoch), int(turn_seq))
        if key in self._entries:
            return False
        self._entries[key] = max(0, int(tokens))
        return True

    def spent_for_agent(self, agent: str) -> int:
        agent = str(agent)
        return sum(t for (a, _e, _s), t in self._entries.items() if a == agent)

    def spent_for_agents(self, agents: frozenset[str] | set[str] | tuple[str, ...]) -> int:
        names = {str(a) for a in agents}
        return sum(t for (a, _e, _s), t in self._entries.items() if a in names)

    def spent_total(self) -> int:
        return sum(self._entries.values())

    def snapshot(self) -> list[dict[str, int | str]]:
        """Flat, JSON-serialisable rows for persistence to the engagement
        dir. Order-independent; the skin round-trips this so the ledger
        survives a daemon restart (else restart would be another laundering
        path)."""
        return [
            {"agent": a, "epoch": e, "turn_seq": s, "tokens": t}
            for (a, e, s), t in sorted(self._entries.items())
        ]

    @classmethod
    def from_snapshot(cls, rows: list[dict] | None) -> BudgetLedger:
        led = cls()
        for row in rows or []:
            try:
                led._entries[(str(row["agent"]), int(row["epoch"]), int(row["turn_seq"]))] = max(
                    0, int(row["tokens"])
                )
            except (KeyError, TypeError, ValueError):
                # Skip malformed rows rather than fail the whole boot — a
                # corrupt ledger file must not wedge the daemon, but it must
                # also never silently reset spend to zero (only bad rows drop).
                continue
        return led


def turn_tokens(usage) -> int:
    """Canonical per-turn token charge.

    Counts every reported category — input + output + cache-create +
    cache-read. Excluding a category (e.g. cache reads because they are
    cheap) would create an accounting escape, and the budget is a workload
    /resource floor, not a billing approximation.
    """
    return (
        int(getattr(usage, "input_tokens", 0) or 0)
        + int(getattr(usage, "output_tokens", 0) or 0)
        + int(getattr(usage, "cache_create_tokens", 0) or 0)
        + int(getattr(usage, "cache_read_tokens", 0) or 0)
    )


def evaluate_budget(
    *,
    spent: int,
    ceiling: int | None,
    warn_frac: float,
    on_exhaustion: str,
    scope: str = "agent",
) -> BudgetVerdict:
    """Pure spend→verdict decision.

    ``ceiling`` None/<=0 means enforcement is OFF (today's observational
    behavior). Otherwise: at/over the ceiling → OVER; at/over
    ``warn_frac * ceiling`` → WARN; else OK.
    """
    if ceiling is None or ceiling <= 0:
        return BudgetVerdict(BudgetAction.OK, spent=spent, ceiling=ceiling)

    action = on_exhaustion if on_exhaustion in ON_EXHAUSTION_VALUES else "pause"

    if spent >= ceiling:
        return BudgetVerdict(
            BudgetAction.OVER,
            on_exhaustion=action,
            spent=spent,
            ceiling=ceiling,
            scope=scope,
            reason=(
                f"token budget exhausted: {spent} >= {ceiling} ({scope} scope, action={action})"
            ),
        )

    frac = warn_frac if 0.0 < warn_frac <= 1.0 else 0.8
    if spent >= ceiling * frac:
        return BudgetVerdict(
            BudgetAction.WARN,
            on_exhaustion=action,
            spent=spent,
            ceiling=ceiling,
            scope=scope,
            reason=f"token budget warning: {spent} >= {int(ceiling * frac)} ({scope} scope)",
        )

    return BudgetVerdict(BudgetAction.OK, on_exhaustion=action, spent=spent, ceiling=ceiling)
