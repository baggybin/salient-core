"""End-to-end pins for the token-budget enforcement floor in the runner loop.

Drives a real AgentRunner over a fake backend that reports per-turn usage,
with a minimal daemon that carries the (real) _RunnerFactoryMixin ledger +
budget_charge. Proves the wire behavior the BUDGET control rung depends on:
warn → park, the PreToolUse deny gate, reset anti-laundering, and explicit
resume. The pure ledger arithmetic is pinned in test_budget_ledger.py.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from salient_core.bus import ContextStore
from salient_core.daemon import AgentRunner
from salient_core.daemon._runner_factory import _RunnerFactoryMixin
from salient_core.policy.safeguards import resolve_config
from salient_core.runtime import (
    AgentEvent,
    AssistantEvent,
    TextContent,
    TurnCompletedEvent,
    TurnUsage,
)


class _Backend:
    """Fake backend: one text turn, then a TurnCompletedEvent whose usage is
    ``per_turn`` tokens (split in/out). Records interrupt() calls."""

    def __init__(self, per_turn: int = 2000) -> None:
        self.queries: list[str] = []
        self.interrupts = 0
        self._per_turn = per_turn

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[AgentEvent]:
        yield AssistantEvent(content=(TextContent("working"),), model="fake")
        half = self._per_turn // 2
        yield TurnCompletedEvent(
            turns=1,
            duration_ms=1,
            usage=TurnUsage(input_tokens=half, output_tokens=half, cost_usd=None),
        )

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def get_context_usage(self) -> None:
        return None

    def diagnose_failure(self, agent_name, error, stderr_tail) -> str:  # noqa: ANN001
        return f"{agent_name}: {error}"


class _BudgetDaemon(_RunnerFactoryMixin):
    """Minimal daemon exposing the real ledger + budget_charge seam."""

    def __init__(self, profile: dict) -> None:
        self.profile = profile
        self.runners: dict[str, object] = {}
        self.questions: list[tuple[str, str]] = []

    def add_question(self, agent: str, text: str, job_id=None) -> int:  # noqa: ANN001
        self.questions.append((agent, text))
        return len(self.questions)


async def _drive_one_job(runner: AgentRunner, prompt: str = "do work"):
    fut = asyncio.get_running_loop().create_future()
    job = runner.submit(prompt, future=fut)
    await asyncio.wait_for(fut, timeout=1)
    return job


def _make_runner(name, daemon, backend, tmp_path, *, epoch=1):  # noqa: ANN001
    runner = AgentRunner(
        name=name,
        cfg={},
        backend_factory=lambda: backend,
        idle_timeout=0.0,
    )
    runner._safeguard_config = resolve_config(runner.cfg, None)
    runner._engagement_path = tmp_path
    runner._epoch = epoch
    runner._daemon = daemon
    daemon.runners[name] = runner
    return runner


@pytest.mark.anyio
async def test_over_budget_parks_and_interrupts(tmp_path: Path) -> None:
    # Given: a 500-token ceiling, pause action; a turn that spends 2000.
    daemon = _BudgetDaemon(
        {
            "token_budgets": {
                "sol": {"ceiling_tokens": 500, "warn_frac": 0.8, "on_exhaustion": "pause"}
            }
        }
    )
    backend = _Backend(per_turn=2000)
    runner = _make_runner("sol", daemon, backend, tmp_path)
    await runner.start()

    # When: the agent runs one job that blows the ceiling.
    job = await _drive_one_job(runner)

    # Then: the gate is armed, the runner is PARKED (not stopped), the turn
    # was interrupted, an operator question was filed, and the reply carries
    # the honest PARTIAL note.
    assert runner._budget_gate_armed is True
    assert runner._budget_parked is True
    assert runner.status == "budget_parked"
    assert backend.interrupts >= 1
    assert any("PARKED" in text for _agent, text in daemon.questions)
    assert "[PARTIAL: token budget floor" in (job.result or "")

    # And: while parked, new work is refused (resume required first).
    fut = asyncio.get_running_loop().create_future()
    refused = runner.submit("more work", future=fut)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(fut, timeout=1)

    # AND the refusal is legible WITHOUT a future. `prompt <agent> <text>` (no
    # --wait) passes none, so the exception above is the only signal it ever
    # had — and it never fired. The operator was told "✓ queued task #N" for a
    # prompt that was never enqueued and was gone after resume.
    assert refused.error is not None
    assert "budget-parked" in refused.error
    fire_and_forget = runner.submit("more work, unwatched")
    assert fire_and_forget.error is not None, (
        "a refused submit must report the refusal on the job it returns — "
        "the fire-and-forget path has no future to fail"
    )
    assert "budget-parked" in fire_and_forget.error
    assert runner.queue.empty(), "a refused job must not be enqueued"

    await runner.stop()
    if runner._task is not None:
        await runner._task


@pytest.mark.anyio
async def test_reset_does_not_launder_spend(tmp_path: Path) -> None:
    # Given: a runner that already spent 2000 against a 500 ceiling on epoch 1.
    daemon = _BudgetDaemon(
        {"token_budgets": {"sol": {"ceiling_tokens": 500, "on_exhaustion": "pause"}}}
    )
    b1 = _Backend(per_turn=2000)
    r1 = _make_runner("sol", daemon, b1, tmp_path, epoch=1)
    await r1.start()
    await _drive_one_job(r1)
    assert r1._budget_parked is True
    await r1.stop()
    if r1._task is not None:
        await r1._task

    # When: a reset — a NEW runner incarnation (fresh epoch) on the SAME
    # daemon/ledger — runs even a tiny 2-token turn.
    b2 = _Backend(per_turn=2)
    r2 = _make_runner("sol", daemon, b2, tmp_path, epoch=2)
    await r2.start()
    await _drive_one_job(r2)

    # Then: it re-parks immediately — the prior spend survived the reset, so
    # the ceiling could not be laundered by recreating the runner.
    assert r2._budget_parked is True
    await r2.stop()
    if r2._task is not None:
        await r2._task


class _RestartableDaemon(_BudgetDaemon):
    """`_BudgetDaemon` plus the two things that actually survive a restart: the
    skin's ledger-persistence hooks and a durable context store (which is where
    the per-agent runner epoch lives)."""

    def __init__(self, profile: dict, db: Path, ledger_file: Path) -> None:
        super().__init__(profile)
        self.context = ContextStore(db)
        self._ledger_file = ledger_file

    def _budget_load(self) -> list[dict] | None:
        if not self._ledger_file.exists():
            return None
        return json.loads(self._ledger_file.read_text())

    def _budget_persist(self, snapshot: list[dict]) -> None:
        self._ledger_file.write_text(json.dumps(snapshot))


@pytest.mark.anyio
async def test_daemon_restart_does_not_launder_spend(tmp_path: Path) -> None:
    """The restart twin of `test_reset_does_not_launder_spend`.

    Regression pin for the 2026-07-25 defect: the runner epoch reset with the
    PROCESS, so after a restart the ledger key `(agent, epoch, turn_seq)`
    repeated, `charge()` returned False (its idempotency guard firing on what
    was really a new turn), and the tokens vanished. Measured on the live
    engagement: 662,254 tokens never charged. Ledger persistence was already
    correct — the KEY was the leak.
    """
    db = tmp_path / "state.db"
    ledger_file = tmp_path / "budget_ledger.json"
    # Ceiling high enough that nothing parks: this pins ACCOUNTING, not
    # enforcement (enforcement is pinned above).
    profile = {"token_budgets": {"sol": {"ceiling_tokens": 100_000, "on_exhaustion": "pause"}}}

    async def one_incarnation() -> int:
        daemon = _RestartableDaemon(profile, db, ledger_file)
        epoch = daemon._allocate_runner_epoch("sol")
        runner = _make_runner("sol", daemon, _Backend(per_turn=2000), tmp_path, epoch=epoch)
        await runner.start()
        await _drive_one_job(runner)
        await runner.stop()
        if runner._task is not None:
            await runner._task
        return daemon._budget_ledger_get().spent_for_agent("sol")

    assert await one_incarnation() == 2000
    # Both turns counted. Before the fix the second incarnation re-issued
    # epoch 1 / turn_seq 1 and this stayed at 2000 — restart-as-laundering.
    assert await one_incarnation() == 4000


@pytest.mark.anyio
async def test_restart_does_not_launder_ENFORCEMENT(tmp_path: Path) -> None:
    """The gap between the two pins above: enforcement state across a restart.

    `test_over_budget_parks_and_interrupts` proves enforcement arms, but never
    restarts. `test_daemon_restart_does_not_launder_spend` restarts, but sets
    "Ceiling high enough that nothing parks" — by its own comment it pins
    ACCOUNTING and defers enforcement to the other test. Each half is pinned
    alone; their INTERSECTION was pinned by nothing.

    And the intersection is where the floor fails. `_budget_gate_armed` and
    `_budget_parked` are plain runner fields defaulting to False, set only from
    the live post-turn charge path and cleared only by `budget_resume()`.
    Nothing re-derives them at boot from the (correctly persisted) ledger. So a
    fresh incarnation of an agent that is massively over its ceiling comes back
    accepting prompts with its PreToolUse deny gate disarmed — one full turn of
    unbounded tool calls per restart, past a ceiling set to prevent exactly
    that, repeatable because restarts are an operator lever.

    The brake must be re-armed by the persisted spend, not by having personally
    witnessed the turn that crossed the line.
    """
    db = tmp_path / "state.db"
    ledger_file = tmp_path / "budget_ledger.json"
    profile = {"token_budgets": {"sol": {"ceiling_tokens": 500, "on_exhaustion": "pause"}}}

    # Incarnation 1: burn 2000 against a 500 ceiling, so it parks and arms.
    first = _RestartableDaemon(profile, db, ledger_file)
    epoch = first._allocate_runner_epoch("sol")
    r1 = _make_runner("sol", first, _Backend(per_turn=2000), tmp_path, epoch=epoch)
    await r1.start()
    await _drive_one_job(r1)
    await r1.stop()
    if r1._task is not None:
        await r1._task

    assert r1._budget_gate_armed is True, "precondition: incarnation 1 must arm the gate"
    assert r1._budget_parked is True, "precondition: incarnation 1 must park"

    # Restart. The ledger persisted, so the spend is still 4x the ceiling.
    second = _RestartableDaemon(profile, db, ledger_file)
    assert second._budget_ledger_get().spent_for_agent("sol") == 2000
    epoch2 = second._allocate_runner_epoch("sol")
    r2 = _make_runner("sol", second, _Backend(per_turn=2000), tmp_path, epoch=epoch2)
    await r2.start()
    for _ in range(200):  # let _run reach the arming point
        if r2.status in ("idle", "budget_parked"):
            break
        await asyncio.sleep(0.01)

    try:
        # The operator set a hard ceiling of 500 and this agent has spent 2000.
        # Nothing about a process boundary should hand it another turn.
        assert r2._budget_gate_armed is True, (
            "RESTART LAUNDERS ENFORCEMENT: spend (2000) is 4x the ceiling (500) "
            "and persisted correctly, but the PreToolUse deny gate came back "
            "disarmed — every tool call in the next turn is permitted"
        )
        assert r2._budget_parked is True, (
            "RESTART LAUNDERS ENFORCEMENT: an agent over its hard ceiling came "
            "back accepting prompts (submit() only refuses on _budget_parked)"
        )

        # The observe half. `idle` reads as "ready for prompts" on every operator
        # surface, which is how this stayed invisible on a live engagement.
        assert r2.status == "budget_parked", (
            f"an over-ceiling agent reported status {r2.status!r} after a restart"
        )

        # The floor itself: the PreToolUse deny gate must refuse tool calls.
        hook = second._make_budget_gate_hook("sol")
        outcome = await hook({"tool_name": "Bash", "tool_input": {}}, "tu-1", None)
        decision = (outcome or {}).get("hookSpecificOutput", {}).get("permissionDecision")
        assert decision == "deny", (
            f"the PreToolUse budget gate permitted a tool call for an agent 4x "
            f"over its ceiling after a restart (decision={decision!r})"
        )

        # And the operator-visible consequence: no new work is admitted.
        refused = r2.submit("more work")
        assert refused.error and "budget-parked" in refused.error, (
            f"a restarted over-ceiling agent accepted a job (error={refused.error!r})"
        )
    finally:
        await r2.stop()
        if r2._task is not None:
            await r2._task


@pytest.mark.anyio
async def test_budget_resume_clears_gate(tmp_path: Path) -> None:
    daemon = _BudgetDaemon(
        {"token_budgets": {"sol": {"ceiling_tokens": 500, "on_exhaustion": "pause"}}}
    )
    backend = _Backend(per_turn=2000)
    runner = _make_runner("sol", daemon, backend, tmp_path)
    await runner.start()
    await _drive_one_job(runner)
    assert runner._budget_parked is True

    # When: the operator resumes (after raising the ceiling / crediting).
    await runner.budget_resume()

    # Then: gate + park cleared, status back to idle, admits work again.
    assert runner._budget_gate_armed is False
    assert runner._budget_parked is False
    assert runner.status == "idle"

    await runner.stop()
    if runner._task is not None:
        await runner._task


@pytest.mark.anyio
async def test_budget_gate_hook_denies_only_when_armed(tmp_path: Path) -> None:
    daemon = _BudgetDaemon({})
    backend = _Backend()
    runner = _make_runner("sol", daemon, backend, tmp_path)
    hook = daemon._make_budget_gate_hook("sol")

    # Disarmed: pass-through.
    assert await hook({}, "tid", None) == {}

    # Armed: every tool call is denied (without touching in-flight work).
    runner._budget_gate_armed = True
    out = await hook({}, "tid", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.anyio
async def test_budget_gate_records_park_block_when_armed(tmp_path: Path) -> None:
    """T3.1 H2: an armed budget gate records a `budget_park` block on the same
    PreToolUse path as the deny (best-effort, keyed by the turn's correlation
    id), so a parked turn no longer reconstructs as a chain that just ends. No
    block row while disarmed."""
    from types import SimpleNamespace

    daemon = _BudgetDaemon({})
    daemon.context = ContextStore(tmp_path / "state.db", engagement_id="op")
    backend = _Backend()
    runner = _make_runner("sol", daemon, backend, tmp_path)
    runner.current = SimpleNamespace(correlation_id="op:sol:1:1")
    hook = daemon._make_budget_gate_hook("sol")
    try:
        # Disarmed: pass-through, no block row.
        assert await hook({"tool_name": "curl"}, "tu-0", None) == {}
        assert daemon.context.load_audit_mirror("op:sol:1:1") == []

        # Armed: deny AND a budget_park block row for the blocked call.
        runner._budget_gate_armed = True
        out = await hook({"tool_name": "curl"}, "tu-1", None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        rows = daemon.context.load_audit_mirror("op:sol:1:1")
        assert len(rows) == 1
        assert rows[0]["op"] == "budget_park"
        assert rows[0]["tool"] == "curl"
        assert rows[0]["tool_use_id"] == "tu-1"
    finally:
        daemon.context.close()


@pytest.mark.anyio
async def test_warn_threshold_fires_question_once_then_parks(tmp_path: Path) -> None:
    # Ceiling 2500, warn at 0.8 → 2000. A 2000-token turn is a WARN (not over).
    daemon = _BudgetDaemon(
        {
            "token_budgets": {
                "sol": {"ceiling_tokens": 2500, "warn_frac": 0.8, "on_exhaustion": "pause"}
            }
        }
    )
    backend = _Backend(per_turn=2000)
    runner = _make_runner("sol", daemon, backend, tmp_path)
    await runner.start()

    await _drive_one_job(runner)
    assert runner._budget_warned is True
    assert runner._budget_parked is False  # warn only — still running
    assert sum("nearing its token budget" in text for _a, text in daemon.questions) == 1

    # Second turn: cumulative 4000 >= 2500 → park.
    await _drive_one_job(runner)
    assert runner._budget_parked is True

    await runner.stop()
    if runner._task is not None:
        await runner._task
