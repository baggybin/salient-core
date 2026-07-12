from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from salient_core.bus import ContextStore
from salient_core.coord.questions import QuestionInbox
from salient_core.daemon import AgentRunner, Job
from salient_core.daemon._runner_factory import _RunnerFactoryMixin
from salient_core.memory.kg import KnowledgeGraph
from salient_core.policy import scope
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguards import SafeguardConfig


class _HookRunner:
    def __init__(self, dataset: PolicyDataset, *, enforce: bool = True) -> None:
        self.cfg: dict[str, Any] = {"enforce_builtin_policy": enforce}
        self._policy_dataset = dataset
        self._safeguard_config = SafeguardConfig()
        self._enforce_builtin_policy = enforce
        self._legacy_trusted_builtin_warned: set[str] = set()
        self._legacy_trusted_builtin_warning_lock = anyio.Lock()
        self.total_safeguard_blocks = 0
        self.options = SimpleNamespace(tools=["Read", "Bash"])
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def _record_jsonl(self, event: str, payload: dict[str, Any]) -> None:
        json.dumps(payload)
        self.records.append((event, payload))


class _HookDaemon(_RunnerFactoryMixin):
    def __init__(self, runner: _HookRunner, store: scope.ScopeStore) -> None:
        self.runners = {"matrix-agent": runner}
        self.scope = store
        self.profile: dict[str, Any] = {}


@dataclass(frozen=True)
class _Tool:
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class _TextDaemon:
    context: ContextStore
    kg: KnowledgeGraph
    inbox: QuestionInbox
    engagement_path: Path

    def add_question(self, agent: str, text: str, job_id: int | None = None) -> int:
        return self.inbox.add(agent, text, job_id=job_id or 0).id


def _dataset(
    targets: dict[str, scope.ExtractorSpec],
    *,
    prohibited: dict[str, list[tuple[str, str]]] | None = None,
    loud: dict[str, list[tuple[str, str]]] | None = None,
) -> PolicyDataset:
    return PolicyDataset(
        tool_targets=targets,
        prohibited_patterns=prohibited or {},
        loud_patterns=loud or {},
    )


def _decision(result: dict[str, Any]) -> str | None:
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


def _row_count(store: scope.ScopeStore) -> int:
    if store._conn is None:
        return 0
    return int(store._conn.execute("SELECT COUNT(*) FROM scope_decisions").fetchone()[0])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("spec", "args", "in_scope", "expected", "rows"),
    [
        (scope.ExtractorSpec(none=True), {}, False, "allow", 0),
        (scope.ExtractorSpec(local_only=True), {}, False, "allow", 1),
        (
            scope.ExtractorSpec(fields={"target": "host"}),
            {"target": "ok.example"},
            True,
            "allow",
            1,
        ),
        (
            scope.ExtractorSpec(fields={"target": "host"}),
            {"target": "no.example"},
            False,
            "deny",
            1,
        ),
        (None, {"password": "matrix-secret"}, False, "deny", 1),
    ],
    ids=["none", "local-only", "target-allow", "target-deny", "unclassified"],
)
async def test_internal_and_external_mcp_share_scope_classification_matrix(
    tmp_path: Path,
    spec: scope.ExtractorSpec | None,
    args: dict[str, Any],
    in_scope: bool,
    expected: str,
    rows: int,
) -> None:
    # Given equivalent internal and external MCP classifications.
    stores = [
        scope.ScopeStore(tmp_path / "internal.db", "matrix-agent"),
        scope.ScopeStore(tmp_path / "external.db", "matrix-agent"),
    ]
    dispatched: list[dict[str, Any]] = []

    async def handler(tool_args: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(tool_args)
        return {"ok": True}

    targets = {} if spec is None else {"alpha.scan": spec}
    dataset = _dataset(targets)
    try:
        if in_scope:
            for store in stores:
                store.add_adhoc("ok.example", reason="matrix")
        internal = scope.gate(
            _Tool(handler), "scan", "matrix-agent", stores[0], "alpha", dataset=dataset
        )
        external_runner = _HookRunner(dataset)
        external = _HookDaemon(external_runner, stores[1])._make_external_scope_hook(
            "matrix-agent", {"alpha"}
        )

        # When both real adapters evaluate the same logical invocation.
        internal_result = await internal.handler(args)
        external_result = await external(
            {"tool_name": "mcp__alpha__scan", "tool_input": args}, "external-id", None
        )

        # Then verdict, dispatch ordering, audit cardinality, and redaction agree.
        assert ("is_error" in internal_result) is (expected == "deny")
        assert _decision(external_result) == expected
        assert len(dispatched) == (1 if expected == "allow" else 0)
        assert [_row_count(store) for store in stores] == [rows, rows]
        assert "matrix-secret" not in repr(
            [store._conn.execute("SELECT * FROM scope_decisions").fetchall() for store in stores]
        )
    finally:
        for store in stores:
            store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("spec", "enforce", "expected", "event", "rows"),
    [
        (scope.ExtractorSpec(none=True), True, None, None, 0),
        (scope.ExtractorSpec(local_only=True), True, None, None, 1),
        (scope.ExtractorSpec(fields={"target": "host"}), True, "deny", "builtin_policy_deny", 1),
        (None, False, None, "builtin_policy_shadow", 1),
        (None, True, "deny", "builtin_policy_deny", 1),
    ],
    ids=["none", "local-only", "target-deny", "unclassified-shadow", "unclassified-enforce"],
)
async def test_sdk_policy_and_dispatch_verdict_matrix(
    tmp_path: Path,
    spec: scope.ExtractorSpec | None,
    enforce: bool,
    expected: str | None,
    event: str | None,
    rows: int,
) -> None:
    # Given one SDK capability whose classification and rollout mode vary.
    targets = {} if spec is None else {"builtin.Read": spec}
    store = scope.ScopeStore(tmp_path / "sdk.db", "matrix-agent")
    runner = _HookRunner(_dataset(targets), enforce=enforce)
    try:
        # When the real SDK pre-tool hook evaluates it.
        result = await _HookDaemon(runner, store)._make_safeguard_hook("matrix-agent")(
            {
                "tool_name": "Read",
                "tool_input": {"target": "denied.example", "password": "matrix-secret"},
            },
            "sdk-id",
            None,
        )

        # Then policy and effective dispatch remain distinct only in shadow mode.
        assert _decision(result) == expected
        assert [name for name, _payload in runner.records] == ([] if event is None else [event])
        assert _row_count(store) == rows
        assert "matrix-secret" not in repr(runner.records)
    finally:
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("gate", "event", "delta"),
    [
        ("safeguard", "safeguard_block", 1),
        ("posture", "safeguard_posture_gate", 0),
        ("halt", "safeguard_halt_blocked", 0),
    ],
)
@pytest.mark.parametrize(
    ("tool", "qualified"),
    [("Read", "builtin.Read"), ("mcp__alpha__scan", "alpha.scan")],
    ids=["sdk", "mcp"],
)
async def test_safeguard_matrix_precedes_sdk_and_mcp_dispatch(
    gate: str,
    event: str,
    delta: int,
    tool: str,
    qualified: str,
) -> None:
    # Given a classified SDK or MCP invocation stopped by one universal gate.
    prohibited = {qualified: [("blocked", "matrix-secret")]} if gate == "safeguard" else {}
    loud = {qualified: [("noisy", "matrix-secret")]} if gate == "posture" else {}
    runner = _HookRunner(
        _dataset({qualified: scope.ExtractorSpec(none=True)}, prohibited=prohibited, loud=loud)
    )
    runner._safeguard_config.posture = "stealth" if gate == "posture" else "normal"
    runner.total_safeguard_blocks = 3 if gate == "halt" else 0

    # When the shared real hook evaluates the call.
    result = await _HookDaemon(runner, scope.ScopeStore(None, gate))._make_safeguard_hook(
        "matrix-agent"
    )(
        {"tool_name": tool, "tool_input": {"password": "matrix-secret"}},
        f"{gate}-{tool}",
        None,
    )

    # Then it denies once, increments at most once, and never leaks the raw input.
    assert _decision(result) == "deny"
    assert [name for name, _payload in runner.records] == [event]
    assert runner.total_safeguard_blocks == (3 if gate == "halt" else delta)
    assert "matrix-secret" not in repr(runner.records)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("posture_denied", "event", "rows"),
    [(False, "text_policy_deny", 1), (True, "safeguard_posture_gate", 0)],
)
async def test_text_denial_precedes_mutation_and_execution_records(
    tmp_path: Path, posture_denied: bool, event: str, rows: int
) -> None:
    # Given a real text runner denied by scope or stealth posture.
    context = ContextStore(tmp_path / "context.db", events_cap_per_agent=0)
    kg = KnowledgeGraph(tmp_path / "kg.db")
    inbox = QuestionInbox(context)
    scope_store = scope.ScopeStore(tmp_path / "text-scope.db", "matrix-agent")
    daemon = _TextDaemon(context, kg, inbox, tmp_path / "engagement")
    runner = AgentRunner(name="matrix-agent", cfg={}, context=context)
    runner._daemon = daemon
    runner._scope_store = scope_store
    runner._safeguard_config = SafeguardConfig(posture="stealth")
    spec = (
        scope.ExtractorSpec(none=True)
        if posture_denied
        else scope.ExtractorSpec(fields={"value": "host"})
    )
    loud = {"bus.context_write": [("noisy", "denied.example")]} if posture_denied else {}
    runner._policy_dataset = _dataset({"bus.context_write": spec}, loud=loud)
    runner._enforce_builtin_policy = True
    call = '<function=context_write>{"key":"finding","value":"denied.example"}</function>'
    try:
        # When the real text adapter evaluates and dispatches the call.
        await runner._dispatch_text_function_calls(
            Job(id=1, prompt="matrix", submitted_at=0.0, result=call)
        )

        # Then denial is durable before mutation and has no success execution pair.
        assert context.read("matrix-agent", "finding") is None
        events = context.query_events(agent="matrix-agent", limit=100)
        assert [item["kind"] for item in events] == [event, "tool-error"]
        assert events[0]["content"]["qualified"] == "bus.context_write"
        assert events[0]["content"]["transport"] == "text"
        assert runner.total_safeguard_blocks == 0
        assert _row_count(scope_store) == rows
    finally:
        scope_store.close()
        kg.close()
        context.close()
