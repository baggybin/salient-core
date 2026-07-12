from __future__ import annotations

import json
from typing import Any

import pytest

from salient_core.daemon._runner_factory import _RunnerFactoryMixin
from salient_core.policy import scope
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguards import SafeguardConfig


class _Runner:
    def __init__(self, dataset: PolicyDataset) -> None:
        self.cfg: dict[str, Any] = {}
        self._policy_dataset = dataset
        self._safeguard_config = SafeguardConfig()
        self._enforce_builtin_policy = True
        self.total_safeguard_blocks = 0
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def _record_jsonl(self, event: str, payload: dict[str, Any]) -> None:
        json.dumps(payload)
        self.records.append((event, payload))


class _Daemon(_RunnerFactoryMixin):
    def __init__(self, runner: _Runner, store: scope.ScopeStore) -> None:
        self.runners = {"agent": runner}
        self.scope = store
        self.profile: dict[str, Any] = {}


def _decision(result: dict[str, Any]) -> str | None:
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


@pytest.mark.anyio
@pytest.mark.parametrize("tool_use_id", [None, "", "   ", 7])
async def test_invalid_tool_use_id_denies_safeguard_before_any_effect(tool_use_id: Any) -> None:
    # Given a prohibited SDK invocation without a valid nonempty string ID.
    dataset = PolicyDataset(
        tool_targets={"builtin.Read": scope.ExtractorSpec(none=True)},
        prohibited_patterns={"builtin.Read": [("blocked", "prohibited-marker")]},
        loud_patterns={},
    )
    runner = _Runner(dataset)
    store = scope.ScopeStore(None, "invalid-sdk-id")
    hook = _Daemon(runner, store)._make_safeguard_hook("agent")
    invocation = {"tool_name": "Read", "tool_input": {"password": "prohibited-marker"}}

    # When the malformed callback is repeated.
    first = await hook(invocation, tool_use_id, None)
    replay = await hook(invocation, tool_use_id, None)

    # Then both deny before a strike, audit event, or scope row can occur.
    assert _decision(first) == "deny"
    assert replay == first
    assert "invalid tool_use_id" in first["hookSpecificOutput"]["permissionDecisionReason"]
    assert runner.total_safeguard_blocks == 0
    assert runner.records == []
    assert store._conn is None


@pytest.mark.anyio
@pytest.mark.parametrize("tool_use_id", [None, "", "   ", 7])
async def test_invalid_tool_use_id_denies_external_scope_before_any_row(tool_use_id: Any) -> None:
    # Given a classified external invocation without a valid nonempty string ID.
    dataset = PolicyDataset(
        tool_targets={"alpha.scan": scope.ExtractorSpec(fields={"target": "host"})},
        prohibited_patterns={},
        loud_patterns={},
    )
    runner = _Runner(dataset)
    store = scope.ScopeStore(None, "invalid-external-id")
    hook = _Daemon(runner, store)._make_external_scope_hook("agent", {"alpha"})
    invocation = {"tool_name": "mcp__alpha__scan", "tool_input": {"target": "alpha.example"}}

    # When the malformed callback is repeated.
    first = await hook(invocation, tool_use_id, None)
    replay = await hook(invocation, tool_use_id, None)

    # Then both deny before scope evaluation can persist a row.
    assert _decision(first) == "deny"
    assert replay == first
    assert "invalid tool_use_id" in first["hookSpecificOutput"]["permissionDecisionReason"]
    assert runner.records == []
    assert store._conn is None
