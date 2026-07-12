from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from salient_core.daemon import _runner_factory
from salient_core.daemon._runner_factory import _RunnerFactoryMixin
from salient_core.policy import scope
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguards import SafeguardConfig


class _Runner:
    def __init__(self, dataset: PolicyDataset, *, enforce: bool = False) -> None:
        self.cfg: dict[str, Any] = {"enforce_builtin_policy": enforce}
        self._policy_dataset = dataset
        self._safeguard_config = SafeguardConfig()
        self._enforce_builtin_policy = enforce
        self.total_safeguard_blocks = 0
        self.options = SimpleNamespace(tools=["Read", "Bash"])
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def _record_jsonl(self, event: str, payload: dict[str, Any]) -> None:
        json.dumps(payload)
        self.records.append((event, payload))


class _Daemon(_RunnerFactoryMixin):
    def __init__(self, runner: _Runner, store: scope.ScopeStore) -> None:
        self.runners = {"agent": runner}
        self.scope = store
        self.profile: dict[str, Any] = {}

    def add_tool_approval_question(
        self,
        agent: str,
        tool: str,
        summary: str,
        categories: list[str],
    ) -> tuple[int, Any]:
        del agent, tool, summary, categories
        future = _runner_factory.asyncio.get_running_loop().create_future()
        future.set_result("no operator-declined")
        return 7, future


def _dataset(
    targets: dict[str, scope.ExtractorSpec],
    *,
    prohibited: dict[str, list[tuple[str, str]]] | None = None,
) -> PolicyDataset:
    return PolicyDataset(
        tool_targets=targets,
        prohibited_patterns=prohibited or {},
        loud_patterns={},
    )


def _input(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"tool_name": tool, "tool_input": args}


def _decision(result: dict[str, Any]) -> str | None:
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "input_data",
    [
        None,
        {},
        [],
        {"tool_name": "Read", "tool_input": []},
        {"tool_name": "mcp__alpha__", "tool_input": {}},
    ],
)
async def test_safeguard_hook_ignores_malformed_sdk_hook_envelopes(input_data: Any) -> None:
    # Given a malformed envelope that cannot identify a normalized invocation.
    runner = _Runner(_dataset({}), enforce=True)
    daemon = _Daemon(runner, scope.ScopeStore(None, "malformed"))

    # When the SDK hook boundary receives it.
    result = await daemon._make_safeguard_hook("agent")(input_data, "id", None)

    # Then no partial policy effect is applied.
    assert result == {}
    assert runner.records == []
    assert runner.total_safeguard_blocks == 0


@pytest.mark.anyio
@pytest.mark.parametrize("enforce", [False, True], ids=["shadow", "enforce"])
async def test_sdk_classified_builtin_allows_in_both_rollout_modes(
    tmp_path: Path,
    enforce: bool,
) -> None:
    # Given an exposed SDK built-in with an explicit scope classification.
    store = scope.ScopeStore(tmp_path / "scope.db", "sdk-classified")
    runner = _Runner(
        _dataset({"builtin.Read": scope.ExtractorSpec(local_only=True)}), enforce=enforce
    )
    daemon = _Daemon(runner, store)
    try:
        # When the real safeguard hook closure evaluates it.
        result = await daemon._make_safeguard_hook("agent")(
            _input("Read", {"file_path": "/tmp/a"}), "id", None
        )

        # Then classification, rather than rollout mode, permits dispatch.
        assert result == {}
        assert runner.records == []
    finally:
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("enforce", "expected"),
    [(False, None), (True, "deny")],
    ids=["shadow-allows", "enforce-denies"],
)
async def test_sdk_unclassified_builtin_has_one_redacted_policy_event(
    tmp_path: Path,
    enforce: bool,
    expected: str | None,
) -> None:
    # Given an exposed but unclassified built-in with a nested secret.
    store = scope.ScopeStore(tmp_path / "scope.db", "sdk-unclassified")
    runner = _Runner(_dataset({}), enforce=enforce)
    daemon = _Daemon(runner, store)
    try:
        # When policy evaluates the invocation.
        result = await daemon._make_safeguard_hook("agent")(
            _input("Bash", {"command": "true", "auth": {"password": "raw-secret"}}),
            "id",
            None,
        )

        # Then rollout affects dispatch, but one redacted event records the deny.
        assert _decision(result) == expected
        assert len(runner.records) == 1
        event, payload = runner.records[0]
        assert event == ("builtin_policy_deny" if enforce else "builtin_policy_shadow")
        assert payload["input"]["auth"]["password"] == "<redacted-secret>"
        assert "raw-secret" not in repr(runner.records)
    finally:
        store.close()


@pytest.mark.anyio
async def test_sdk_exposure_alone_never_authorizes() -> None:
    # Given Bash in options.tools but absent from tool_targets.
    store = scope.ScopeStore(None, "exposure")
    runner = _Runner(_dataset({}), enforce=True)

    # When the SDK invokes the exposed capability.
    result = await _Daemon(runner, store)._make_safeguard_hook("agent")(
        _input("Bash", {"command": "true"}), "id", None
    )

    # Then policy denies despite exposure.
    assert "Bash" in runner.options.tools
    assert _decision(result) == "deny"


@pytest.mark.anyio
async def test_sdk_prohibited_intent_applies_one_event_and_counter_delta() -> None:
    # Given a classified SDK tool whose raw input matches one prohibited marker.
    dataset = _dataset(
        {"builtin.Read": scope.ExtractorSpec(local_only=True)},
        prohibited={"builtin.Read": [("blocked-marker", "prohibited-token")]},
    )
    runner = _Runner(dataset, enforce=True)
    daemon = _Daemon(runner, scope.ScopeStore(None, "prohibited"))

    # When it is evaluated once.
    result = await daemon._make_safeguard_hook("agent")(
        _input("Read", {"password": "prohibited-token"}), "id", None
    )

    # Then one strike and one redacted safeguard event are applied.
    assert _decision(result) == "deny"
    assert runner.total_safeguard_blocks == 1
    assert [event for event, _payload in runner.records] == ["safeguard_block"]
    assert "prohibited-token" not in repr(runner.records)


@pytest.mark.anyio
async def test_sticky_halt_denies_clean_sdk_builtin_without_increment() -> None:
    # Given a runner already at the sticky-halt threshold.
    runner = _Runner(_dataset({"builtin.Read": scope.ExtractorSpec(local_only=True)}))
    runner.total_safeguard_blocks = 3
    daemon = _Daemon(runner, scope.ScopeStore(None, "halted"))

    # When a clean SDK-native call follows.
    result = await daemon._make_safeguard_hook("agent")(
        _input("Read", {"file_path": "/tmp/a"}), "id", None
    )

    # Then halt is universal and does not add another strike.
    assert _decision(result) == "deny"
    assert runner.total_safeguard_blocks == 3
    assert [event for event, _payload in runner.records] == ["safeguard_halt_blocked"]


@pytest.mark.anyio
async def test_external_scope_prefers_qualified_specs_and_retains_bare_fallback(
    tmp_path: Path,
) -> None:
    # Given colliding external scan names and one compatibility bare spec.
    store = scope.ScopeStore(tmp_path / "scope.db", "external")
    for target in ("alpha.example", "192.0.2.8", "fallback.example"):
        store.add_adhoc(target, reason="test")
    runner = _Runner(
        _dataset(
            {
                "alpha.scan": scope.ExtractorSpec(fields={"alpha": "host"}),
                "beta.scan": scope.ExtractorSpec(fields={"beta": "ip_or_host"}),
                "scan": scope.ExtractorSpec(fields={"fallback": "host"}),
            }
        )
    )
    daemon = _Daemon(runner, store)
    hook = daemon._make_external_scope_hook("agent", {"alpha", "beta", "gamma"})
    try:
        # When each external identity is evaluated.
        results = [
            await hook(_input("mcp__alpha__scan", {"alpha": "alpha.example"}), "a", None),
            await hook(_input("mcp__beta__scan", {"beta": "192.0.2.8"}), "b", None),
            await hook(_input("mcp__gamma__scan", {"fallback": "fallback.example"}), "c", None),
        ]

        # Then all resolve through qualified-first/bare-fallback shared policy.
        assert [_decision(result) for result in results] == ["allow", "allow", "allow"]
        assert store._conn is not None
        rows = list(store._conn.execute("SELECT targets_json FROM scope_decisions"))
        assert [json.loads(row[0])[0]["value"] for row in rows] == [
            "alpha.example",
            "192.0.2.8",
            "fallback.example",
        ]
    finally:
        store.close()


@pytest.mark.anyio
async def test_external_hook_leaves_internal_mcp_scope_to_handler() -> None:
    # Given an internal MCP tool that is absent from the external server set.
    store = scope.ScopeStore(None, "internal")
    daemon = _Daemon(_Runner(_dataset({"internal.scan": scope.ExtractorSpec(none=True)})), store)

    # When the external hook observes it.
    result = await daemon._make_external_scope_hook("agent", {"alpha"})(
        _input("mcp__internal__scan", {}), "id", None
    )

    # Then the hook neither decides nor writes a second scope row.
    assert result == {}
    assert store._conn is None


@pytest.mark.anyio
async def test_external_scope_keeps_strict_behavior_for_research_specs() -> None:
    # Given a public target whose spec would pass only through the research lane.
    store = scope.ScopeStore(None, "external-strict")
    dataset = _dataset(
        {"alpha.scan": scope.ExtractorSpec(fields={"target": "ip_or_host"}, research=True)}
    )
    daemon = _Daemon(_Runner(dataset), store)

    # When the external adapter evaluates it.
    result = await daemon._make_external_scope_hook("agent", {"alpha"})(
        _input("mcp__alpha__scan", {"target": "8.8.8.8"}), "id", None
    )

    # Then external MCP remains strict instead of widening into research scope.
    assert _decision(result) == "deny"


@pytest.mark.anyio
async def test_read_containment_can_deny_after_general_policy_allow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a classified Read and a narrower study-root containment policy.
    runner = _Runner(_dataset({"builtin.Read": scope.ExtractorSpec(local_only=True)}), enforce=True)
    daemon = _Daemon(runner, scope.ScopeStore(None, "read-containment"))
    config = SimpleNamespace(work_root=lambda: tmp_path)
    monkeypatch.setattr(_runner_factory, "get_daemon_skin_module", lambda _name: config)
    call = _input("Read", {"file_path": str(tmp_path / "outside.txt")})

    # When the general and narrower hooks compose in registration order.
    general = await daemon._make_safeguard_hook("agent")(call, "id", None)
    contained = await daemon._make_read_containment_hook("agent")(call, "id", None)

    # Then general policy allows while containment independently denies.
    assert general == {}
    assert _decision(contained) == "deny"


@pytest.mark.anyio
async def test_approve_before_can_deny_after_general_policy_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a classified Bash whose narrower action class requires approval.
    runner = _Runner(_dataset({"builtin.Bash": scope.ExtractorSpec(none=True)}), enforce=True)
    runner.cfg["policy"] = {"approve_before": ["sudo"]}
    daemon = _Daemon(runner, scope.ScopeStore(None, "approve-before"))
    action_class = SimpleNamespace(classify_tool_action=lambda _type, _tool, _input: {"sudo"})
    monkeypatch.setattr(_runner_factory, "get_daemon_skin_module", lambda _name: action_class)
    call = _input("Bash", {"command": "sudo true"})

    # When the general and approval hooks both evaluate the same SDK call.
    general = await daemon._make_safeguard_hook("agent")(call, "id", None)
    approved = await daemon._make_approve_before_hook("agent")(call, "id", None)

    # Then explicit classification does not bypass the operator denial.
    assert general == {}
    assert _decision(approved) == "deny"
