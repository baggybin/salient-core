"""Pins for the DURABLE runner epoch and the correlation-id shape it fences.

Regression cover for the 2026-07-25 defect: the runner epoch was a
process-global counter, so a daemon restart re-issued epoch 1, 2, 3… in
agent-start order. Two durable records keyed off it then collided —

  * correlation ids (`<engagement>:<epoch>:<seq>` at the time) were re-minted,
    so `reconstruct` stitched unrelated turns (even turns belonging to two
    DIFFERENT agents) into one chain and reported it COMPLETE;
  * the budget ledger's idempotency key `(agent, epoch, turn_seq)` collided, so
    `charge()` no-opped and the turn's tokens were never counted — spend
    laundered by restarting the daemon.

The epoch is now per-agent and persisted in the context store's `meta` table.
The id carries the agent, so two agents holding the same epoch is unambiguous.
"""

from __future__ import annotations

from pathlib import Path

from salient_core.bus import ContextStore
from salient_core.daemon import AgentRunner
from salient_core.daemon._runner_factory import _RunnerFactoryMixin


class _Daemon(_RunnerFactoryMixin):
    """Minimal daemon carrying just the real epoch allocator + a context."""

    def __init__(self, context: ContextStore | None) -> None:
        self.context = context
        self.runners: dict[str, object] = {}


def _restart(db: Path) -> _Daemon:
    """A NEW daemon object over the SAME database — what a restart looks like
    to the allocator (fresh process state, durable store)."""
    return _Daemon(ContextStore(db))


# ── the durable epoch ────────────────────────────────────────────────────


def test_epoch_survives_a_daemon_restart(tmp_path: Path) -> None:
    db = tmp_path / "state.db"

    first = _restart(db)._allocate_runner_epoch("sol")
    second = _restart(db)._allocate_runner_epoch("sol")
    third = _restart(db)._allocate_runner_epoch("sol")

    # Strictly increasing across processes — the property the old
    # process-global counter did NOT have (it returned 1, 1, 1).
    assert [first, second, third] == [1, 2, 3]


def test_epoch_is_per_agent_not_global(tmp_path: Path) -> None:
    d = _Daemon(ContextStore(tmp_path / "state.db"))

    # Each agent gets its own sequence; two agents may both hold epoch 1
    # because every consumer's key (correlation id, ledger key, event key)
    # already carries the agent.
    assert d._allocate_runner_epoch("sol") == 1
    assert d._allocate_runner_epoch("luna") == 1
    assert d._allocate_runner_epoch("sol") == 2
    assert d._allocate_runner_epoch("luna") == 2


def test_epoch_monotonic_without_a_database(tmp_path: Path) -> None:
    # No db_path: the store is memory-only, so durability is impossible. The
    # allocator must still be monotonic within the process rather than raise.
    d = _Daemon(ContextStore(None))
    assert [d._allocate_runner_epoch("sol") for _ in range(3)] == [1, 2, 3]

    # And with no context at all (bare daemon in a unit test).
    bare = _Daemon(None)
    assert [bare._allocate_runner_epoch("sol") for _ in range(3)] == [1, 2, 3]


def test_failed_persist_never_reissues_an_epoch(tmp_path: Path) -> None:
    """A store that accepts reads but fails writes must not restart the
    sequence — the in-process high-water mark floors it."""

    class _WriteFails(ContextStore):
        def meta_set(self, key: str, value: str) -> None:
            raise RuntimeError("disk full")

    d = _Daemon(_WriteFails(tmp_path / "state.db"))
    assert [d._allocate_runner_epoch("sol") for _ in range(3)] == [1, 2, 3]


def test_first_allocation_clears_pre_upgrade_ledger_epochs(tmp_path: Path) -> None:
    """Migration seam: epochs minted by the OLD process-local counter are
    already on disk in the budget ledger and start at 1. Beginning the durable
    sequence at 1 would collide with them, and the ledger would drop the very
    first charge after the upgrade — the same bug, at the upgrade boundary."""
    d = _Daemon(ContextStore(tmp_path / "state.db"))
    led = d._budget_ledger_get()
    led.charge("manager", 1, 1, 500)  # pre-upgrade entries
    led.charge("manager", 2, 1, 700)

    # Seeded above the highest epoch already on disk for that agent …
    assert d._allocate_runner_epoch("manager") == 3
    # … per agent: an agent with no history still starts at 1.
    assert d._allocate_runner_epoch("fresh") == 1
    # And the seed happens once — thereafter the persisted counter drives it.
    assert d._allocate_runner_epoch("manager") == 4


def test_garbled_meta_row_does_not_restart_the_sequence(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    d = _Daemon(ContextStore(db))
    assert d._allocate_runner_epoch("sol") == 1

    # Corrupt the persisted row, then re-allocate in the SAME process: the
    # in-process high-water mark still floors it, so the epoch advances.
    d.context.meta_set("runner_epoch:sol", "not-a-number")
    assert d._allocate_runner_epoch("sol") == 2


# ── the correlation id ───────────────────────────────────────────────────


def _runner(name: str, epoch: int, engagement: Path) -> AgentRunner:
    r = AgentRunner(name=name, cfg={}, backend_factory=lambda: None, idle_timeout=0.0)
    r._engagement_path = engagement
    r._epoch = epoch
    return r


def test_correlation_id_shape(tmp_path: Path) -> None:
    eng = tmp_path / "20260724T190327"
    job = _runner("red_lead", 3, eng).submit("work")

    # <engagement>:<agent>:<epoch>:<seq> — four parts, agent included, so an id
    # names whose turn it is instead of being anonymous.
    assert job.correlation_id == "20260724T190327:red_lead:3:1"
    assert len(job.correlation_id.split(":")) == 4


def test_two_agents_sharing_an_epoch_do_not_collide(tmp_path: Path) -> None:
    eng = tmp_path / "eng"
    # Exactly the live failure: scanner and nikto both held epoch 4 (one per
    # daemon incarnation) and minted the identical id.
    scanner = _runner("scanner", 4, eng).submit("scan").correlation_id
    nikto = _runner("nikto", 4, eng).submit("probe").correlation_id
    assert scanner != nikto


def test_ids_are_disjoint_across_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    eng = tmp_path / "eng"

    def incarnation() -> set[str]:
        epoch = _restart(db)._allocate_runner_epoch("red_lead")
        r = _runner("red_lead", epoch, eng)
        return {r.submit("a").correlation_id, r.submit("b").correlation_id}

    first, second = incarnation(), incarnation()

    # Before the fix both incarnations minted {…:3:1, …:3:2} and the audit
    # store merged them.
    assert first.isdisjoint(second)
