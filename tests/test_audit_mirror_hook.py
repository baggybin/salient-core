"""T3.1 spine — PreToolUse denies land in state.db's audit_mirror (Stage C).

Drives the real hooks (`_make_safeguard_hook`, `_make_external_scope_hook`)
against a real ContextStore + ScopeStore and asserts:
  - a safeguard block writes an `audit_mirror` op='safeguard_block' row carrying
    the caller's correlation_id;
  - an out-of-scope external MCP call writes op='scope_deny';
  - a replayed tool_use_id does NOT double-count (idempotent);
  - an allowed call writes NO mirror row.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from salient_core.bus import ContextStore
from salient_core.daemon import _runner_factory
from salient_core.daemon._runner_factory import _RunnerFactoryMixin
from salient_core.policy import scope
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguards import SafeguardConfig


class _Job:
    def __init__(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id


class _Runner:
    def __init__(
        self, dataset: PolicyDataset, *, enforce: bool = False, correlation_id: str = "op:1:1"
    ) -> None:
        self.cfg: dict[str, Any] = {"enforce_builtin_policy": enforce}
        self._policy_dataset = dataset
        self._safeguard_config = SafeguardConfig()
        self._enforce_builtin_policy = enforce
        self.total_safeguard_blocks = 0
        self.options = SimpleNamespace(tools=["Read", "Bash"])
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.current = _Job(correlation_id)
        self._legacy_trusted_builtin_warned: set[str] = set()
        self._legacy_trusted_builtin_warning_lock = asyncio.Lock()

    async def _record_jsonl(self, event: str, payload: dict[str, Any]) -> None:
        json.dumps(payload)
        self.records.append((event, payload))


class _Daemon(_RunnerFactoryMixin):
    def __init__(self, runner: _Runner, store: scope.ScopeStore, context: ContextStore) -> None:
        self.runners = {"agent": runner}
        self.scope = store
        self.profile: dict[str, Any] = {}
        self.context = context

    def add_tool_approval_question(self, *a: Any, **k: Any) -> tuple[int, Any]:
        fut = _runner_factory.asyncio.get_running_loop().create_future()
        fut.set_result("no operator-declined")
        return 7, fut


def _dataset(targets, *, prohibited=None) -> PolicyDataset:
    return PolicyDataset(
        tool_targets=targets,
        prohibited_patterns=prohibited or {},
        loud_patterns={},
    )


def _input(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"tool_name": tool, "tool_input": args}


@pytest.mark.anyio
async def test_safeguard_block_writes_audit_mirror(tmp_path: Path) -> None:
    ctx = ContextStore(tmp_path / "state.db", engagement_id="op")
    store = scope.ScopeStore(None, "op")
    dataset = _dataset(
        {"builtin.Read": scope.ExtractorSpec(local_only=True)},
        prohibited={"builtin.Read": [("blocked-marker", "prohibited-token")]},
    )
    runner = _Runner(dataset, enforce=True, correlation_id="op:2:5")
    daemon = _Daemon(runner, store, ctx)
    try:
        result = await daemon._make_safeguard_hook("agent")(
            _input("Read", {"password": "prohibited-token"}), "tu-sg", None
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        rows = ctx.load_audit_mirror("op:2:5")
        assert len(rows) == 1
        assert rows[0]["op"] == "safeguard_block"
        assert rows[0]["tool_use_id"] == "tu-sg"
        # the block reason isn't leaked verbatim into the mirror secret-side
        assert "prohibited-token" not in json.dumps(rows)
    finally:
        ctx.close()
        store.close()


@pytest.mark.anyio
async def test_external_scope_deny_writes_audit_mirror_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ctx = ContextStore(tmp_path / "state.db", engagement_id="op")
    store = scope.ScopeStore(tmp_path / "scope.db", "op")
    store.add_adhoc("in.scope", reason="test")  # only this target is allowed
    runner = _Runner(
        _dataset({"alpha.scan": scope.ExtractorSpec(fields={"alpha": "host"})}),
        correlation_id="op:3:9",
    )
    daemon = _Daemon(runner, store, ctx)
    hook = daemon._make_external_scope_hook("agent", {"alpha"})
    try:
        # out-of-scope target → deny
        r1 = await hook(_input("mcp__alpha__scan", {"alpha": "out.scope"}), "tu-x", None)
        assert r1["hookSpecificOutput"]["permissionDecision"] == "deny"
        rows = ctx.load_audit_mirror("op:3:9")
        assert [x["op"] for x in rows] == ["scope_deny"]
        assert rows[0]["tool"] == "mcp__alpha__scan"

        # replay of the SAME tool_use_id must not double-count
        await hook(_input("mcp__alpha__scan", {"alpha": "out.scope"}), "tu-x", None)
        assert len(ctx.load_audit_mirror("op:3:9")) == 1

        # an ALLOWED call writes no mirror row
        r2 = await hook(_input("mcp__alpha__scan", {"alpha": "in.scope"}), "tu-ok", None)
        assert r2["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert len(ctx.load_audit_mirror("op:3:9")) == 1

        # the authoritative scope.db deny row carries the same correlation_id
        assert store._conn is not None
        cids = [
            row[0]
            for row in store._conn.execute(
                "SELECT correlation_id FROM scope_decisions WHERE verdict='deny'"
            )
        ]
        assert cids == ["op:3:9"]
    finally:
        ctx.close()
        store.close()


@pytest.mark.anyio
async def test_shadow_mode_deny_is_mirrored_as_shadowed(tmp_path: Path) -> None:
    """An unclassified built-in under shadow mode: scope evaluates DENY and
    records it authoritatively, but the hook lets the call run.

    Mirroring nothing there made reconstruct report "deny in scope.db but
    MISSING from the mirror — INCOMPLETE" for a tool that actually executed:
    a false alarm on every such call, and inverted (it reads as a lost denial
    rather than a permitted call). The permissive outcome is recorded instead.
    """
    ctx = ContextStore(tmp_path / "state.db", engagement_id="op")
    store = scope.ScopeStore(tmp_path / "scope.db", "op")
    # `builtin.Bash` is deliberately absent from tool_targets → UNCLASSIFIED.
    runner = _Runner(_dataset({}), enforce=False, correlation_id="op:bash:10:1")
    daemon = _Daemon(runner, store, ctx)
    try:
        result = await daemon._make_safeguard_hook("agent")(
            _input("Bash", {"command": "ls -la"}), "tu-shadow", None
        )

        # The call was PERMITTED (shadow mode returns no deny decision) …
        assert result.get("hookSpecificOutput") is None

        # … and scope.db still holds an authoritative deny for it …
        assert store._conn is not None
        verdicts = [
            row[0]
            for row in store._conn.execute(
                "SELECT verdict FROM scope_decisions WHERE correlation_id='op:bash:10:1'"
            )
        ]
        assert verdicts == ["deny"]

        # … so the mirror must carry a row, marked as shadowed rather than
        # enforced, or the cross-check invents a gap.
        rows = ctx.load_audit_mirror("op:bash:10:1")
        assert [r["op"] for r in rows] == ["scope_deny_shadowed"]
        assert rows[0]["tool"] == "Bash"
        assert rows[0]["tool_use_id"] == "tu-shadow"
    finally:
        ctx.close()
        store.close()


@pytest.mark.anyio
async def test_enforce_mode_deny_is_mirrored_as_enforced(tmp_path: Path) -> None:
    """The twin of the above: with enforcement ON the same call is denied and
    mirrored as a plain `scope_deny`. The two are never conflated."""
    ctx = ContextStore(tmp_path / "state.db", engagement_id="op")
    store = scope.ScopeStore(tmp_path / "scope.db", "op")
    runner = _Runner(_dataset({}), enforce=True, correlation_id="op:bash:10:2")
    daemon = _Daemon(runner, store, ctx)
    try:
        result = await daemon._make_safeguard_hook("agent")(
            _input("Bash", {"command": "ls -la"}), "tu-enforced", None
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        rows = ctx.load_audit_mirror("op:bash:10:2")
        assert [r["op"] for r in rows] == ["scope_deny"]
    finally:
        ctx.close()
        store.close()


if __name__ == "__main__":
    import unittest

    unittest.main()
