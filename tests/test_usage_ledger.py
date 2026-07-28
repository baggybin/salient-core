"""T3.1 spine (PR5) — a real per-turn usage/cost ledger.

Two contracts:
  1. store: record_usage round-trips (tokens + cost + model + correlation),
     no-DB is not a failure, a write failure signals False.
  2. end-to-end: a completed turn writes one usage_ledger row keyed by the job's
     correlation id — driven through a real AgentRunner over a fake backend that
     emits a TurnCompletedEvent (the same handler the budget floor rides).
     Distinct from BudgetLedger, which stays a tokens-only accumulator.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from salient_core.bus import ContextStore
from salient_core.daemon import AgentRunner
from salient_core.policy.safeguards import resolve_config
from salient_core.runtime import (
    AgentEvent,
    AssistantEvent,
    TextContent,
    TurnCompletedEvent,
    TurnUsage,
)


def test_record_usage_round_trip() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContextStore(Path(td) / "state.db", engagement_id="op")
        try:
            cid = "op:1:2"
            assert store.record_usage(
                agent="scout",
                model="claude-x",
                correlation_id=cid,
                input_tokens=100,
                output_tokens=40,
                cache_read_tokens=10,
                cache_create_tokens=5,
                reasoning_tokens=3,
                total_tokens=158,
                cost_usd=0.0123,
            )
            rows = store.load_usage(cid)
            assert len(rows) == 1
            r = rows[0]
            assert r["model"] == "claude-x"
            assert r["input_tokens"] == 100
            assert r["output_tokens"] == 40
            assert r["cache_read_tokens"] == 10
            assert r["reasoning_tokens"] == 3
            assert abs(r["cost_usd"] - 0.0123) < 1e-9
        finally:
            store.close()


def test_load_usage_is_correlation_scoped_not_agent_scoped() -> None:
    """A legacy correlation id can carry rows from more than one agent.

    Ids were minted `<engagement>:<epoch>:<counter>` with a per-runner epoch
    until 2026-07-25, so two agents could mint the same one; `submit()` now
    interposes the agent name, but pre-fix rows are still on disk and both
    shapes coexist in one database. `load_usage` filters on the id ALONE, so
    anything attributing a model to an agent must match `row["agent"]` too —
    on one live engagement 18 rows joined to a different agent's job, and one
    job with no ledger row of its own would have borrowed another agent's
    model. Pinned because a consumer (ModelDistillery's provenance resolver)
    now depends on this being the documented behaviour rather than a bug to
    quietly "fix" by making the query agent-scoped, which would silently
    change what reconstruct sees.
    """
    with tempfile.TemporaryDirectory() as td:
        store = ContextStore(Path(td) / "state.db", engagement_id="op")
        try:
            legacy = "op:4:1"  # three parts: no agent segment
            assert store.record_usage(agent="scanner", model="haiku", correlation_id=legacy)
            assert store.record_usage(agent="nikto", model="opus", correlation_id=legacy)

            rows = store.load_usage(legacy)
            assert len(rows) == 2, "load_usage must not filter by agent"
            assert {r["agent"] for r in rows} == {"scanner", "nikto"}
            by_agent = {r["agent"]: r["model"] for r in rows}
            assert by_agent == {"scanner": "haiku", "nikto": "opus"}
        finally:
            store.close()


def test_record_usage_no_db_is_ok() -> None:
    store = ContextStore(None)
    assert store.record_usage(agent="a", model=None, correlation_id="c")


def test_record_usage_write_failure_signals_false() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContextStore(Path(td) / "state.db")
        try:
            with store._lock:
                store._conn.execute("DROP TABLE usage_ledger")
                store._conn.commit()
            assert store.record_usage(agent="a", model=None, correlation_id="c") is False
            assert store.degraded
        finally:
            store.close()


# ── end-to-end: a turn writes a usage row ──────────────────────────────


class _Backend:
    def __init__(self, usage: TurnUsage) -> None:
        self._usage = usage
        self.queries: list[str] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[AgentEvent]:
        yield AssistantEvent(content=(TextContent("working"),), model="fake")
        yield TurnCompletedEvent(turns=1, duration_ms=1, usage=self._usage)

    async def interrupt(self) -> None:
        return None

    async def get_context_usage(self) -> None:
        return None

    def diagnose_failure(self, agent_name, error, stderr_tail) -> str:  # noqa: ANN001
        return f"{agent_name}: {error}"


@pytest.mark.anyio
async def test_completed_turn_writes_usage_row(tmp_path: Path) -> None:
    ctx = ContextStore(tmp_path / "state.db", engagement_id=tmp_path.name)
    backend = _Backend(
        TurnUsage(
            input_tokens=300,
            output_tokens=120,
            cache_read_tokens=20,
            cache_create_tokens=8,
            cost_usd=0.05,
        )
    )
    runner = AgentRunner(
        name="sol",
        cfg={"model": "fake-model"},
        backend_factory=lambda: backend,
        idle_timeout=0.0,
        context=ctx,
    )
    runner._safeguard_config = resolve_config(runner.cfg, None)
    runner._engagement_path = tmp_path
    runner._epoch = 1
    try:
        await runner.start()
        import asyncio

        fut = asyncio.get_running_loop().create_future()
        job = runner.submit("do work", future=fut)
        await asyncio.wait_for(fut, timeout=2)

        rows = ctx.load_usage(job.correlation_id)
        assert len(rows) == 1, f"expected one usage row for {job.correlation_id}"
        r = rows[0]
        assert r["agent"] == "sol"
        assert r["model"] == "fake-model"
        assert r["input_tokens"] == 300
        assert r["output_tokens"] == 120
        assert abs(r["cost_usd"] - 0.05) < 1e-9
    finally:
        await runner.stop()
        ctx.close()


if __name__ == "__main__":
    import unittest

    unittest.main()
