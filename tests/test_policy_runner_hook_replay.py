from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from salient_core.daemon._runner_factory import _RunnerFactoryMixin
from salient_core.policy import scope
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguards import SafeguardConfig


class _Runner:
    def __init__(self, dataset: PolicyDataset, *, enforce: bool = False) -> None:
        self.cfg: dict[str, Any] = {}
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


class _BlockingRecordRunner(_Runner):
    def __init__(self, dataset: PolicyDataset) -> None:
        super().__init__(dataset, enforce=True)
        self.record_entered = anyio.Event()
        self.release_record = anyio.Event()

    async def _record_jsonl(self, event: str, payload: dict[str, Any]) -> None:
        self.records.append((event, payload))
        self.record_entered.set()
        await self.release_record.wait()


class _FailOnceRecordRunner(_Runner):
    def __init__(self, dataset: PolicyDataset) -> None:
        super().__init__(dataset, enforce=True)
        self.record_attempts = 0

    async def _record_jsonl(self, event: str, payload: dict[str, Any]) -> None:
        self.record_attempts += 1
        if self.record_attempts == 1:
            self.records.append((event, payload))
            raise RuntimeError("injected policy persistence failure")
        await super()._record_jsonl(event, payload)


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
    ("tool", "qualified"),
    [("Read", "builtin.Read"), ("mcp__alpha__scan", "alpha.scan")],
    ids=["sdk", "mcp"],
)
async def test_exact_prohibited_replay_applies_one_strike_and_event(
    tool: str,
    qualified: str,
) -> None:
    # Given a prohibited invocation and one stable provider tool-use ID.
    dataset = _dataset(
        {qualified: scope.ExtractorSpec(none=True)},
        prohibited={qualified: [("blocked", "prohibited-marker")]},
    )
    runner = _Runner(dataset, enforce=True)
    hook = _Daemon(runner, scope.ScopeStore(None, "prohibited-replay"))._make_safeguard_hook(
        "agent"
    )
    invocation = _input(tool, {"password": "prohibited-marker"})

    # When the provider replays the completed invocation exactly.
    first = await hook(invocation, "same-id", None)
    replay = await hook(invocation, "same-id", None)

    # Then the response replays without reapplying its failed-gate effects.
    assert replay == first
    assert _decision(replay) == "deny"
    assert runner.total_safeguard_blocks == 1
    assert [event for event, _payload in runner.records] == ["safeguard_block"]


@pytest.mark.anyio
@pytest.mark.parametrize("enforce", [False, True], ids=["shadow", "enforce"])
async def test_exact_unclassified_sdk_replay_writes_one_policy_event_and_scope_row(
    tmp_path: Path,
    enforce: bool,
) -> None:
    # Given an unclassified SDK invocation under one rollout mode.
    store = scope.ScopeStore(tmp_path / "scope.db", "sdk-replay")
    runner = _Runner(_dataset({}), enforce=enforce)
    hook = _Daemon(runner, store)._make_safeguard_hook("agent")
    invocation = _input("Bash", {"command": "true", "password": "secret"})
    try:
        # When the same provider ID and invocation are delivered twice.
        first = await hook(invocation, "same-id", None)
        replay = await hook(invocation, "same-id", None)

        # Then dispatch response and each durable effect occur exactly once.
        assert replay == first
        assert _decision(replay) == ("deny" if enforce else None)
        assert len(runner.records) == 1
        assert store._conn is not None
        assert store._conn.execute("SELECT COUNT(*) FROM scope_decisions").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.anyio
async def test_exact_external_scope_replay_writes_one_scope_row(tmp_path: Path) -> None:
    # Given a classified external invocation allowed by strict scope.
    store = scope.ScopeStore(tmp_path / "scope.db", "external-replay")
    store.add_adhoc("alpha.example", reason="test")
    runner = _Runner(_dataset({"alpha.scan": scope.ExtractorSpec(fields={"target": "host"})}))
    hook = _Daemon(runner, store)._make_external_scope_hook("agent", {"alpha"})
    invocation = _input("mcp__alpha__scan", {"target": "alpha.example"})
    try:
        # When the completed external hook call is replayed exactly.
        first = await hook(invocation, "same-id", None)
        replay = await hook(invocation, "same-id", None)

        # Then the allow is replayed without a second SQLite row.
        assert replay == first
        assert _decision(replay) == "allow"
        assert store._conn is not None
        assert store._conn.execute("SELECT COUNT(*) FROM scope_decisions").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.anyio
async def test_reused_tool_use_id_with_different_invocation_fails_closed() -> None:
    # Given a completed classified allow cached under one provider ID.
    dataset = _dataset(
        {
            "builtin.Read": scope.ExtractorSpec(none=True),
            "builtin.Bash": scope.ExtractorSpec(none=True),
        }
    )
    runner = _Runner(dataset, enforce=True)
    hook = _Daemon(runner, scope.ScopeStore(None, "id-collision"))._make_safeguard_hook("agent")
    first = await hook(_input("Read", {"file_path": "/tmp/a"}), "reused-id", None)

    # When the provider reuses that ID for a different tool and input.
    collision = await hook(_input("Bash", {"command": "true"}), "reused-id", None)

    # Then the cached allow is not inherited by the mismatched invocation.
    assert first == {}
    assert _decision(collision) == "deny"
    assert "collision" in collision["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.anyio
async def test_distinct_tool_use_ids_remain_distinct_legitimate_calls(tmp_path: Path) -> None:
    # Given the same unclassified shadow invocation with distinct provider IDs.
    store = scope.ScopeStore(tmp_path / "scope.db", "distinct-ids")
    runner = _Runner(_dataset({}))
    hook = _Daemon(runner, store)._make_safeguard_hook("agent")
    invocation = _input("Bash", {"command": "true"})
    try:
        # When each legitimate call carries its own ID.
        await hook(invocation, "id-one", None)
        await hook(invocation, "id-two", None)

        # Then both calls independently apply their expected effects.
        assert len(runner.records) == 2
        assert store._conn is not None
        assert store._conn.execute("SELECT COUNT(*) FROM scope_decisions").fetchone()[0] == 2
    finally:
        store.close()


@pytest.mark.anyio
async def test_concurrent_identical_replay_coalesces_before_policy_effects() -> None:
    # Given a prohibited SDK call whose owner pauses during persistence.
    dataset = _dataset(
        {"builtin.Read": scope.ExtractorSpec(none=True)},
        prohibited={"builtin.Read": [("blocked", "prohibited-marker")]},
    )
    runner = _BlockingRecordRunner(dataset)
    hook = _Daemon(runner, scope.ScopeStore(None, "concurrent-replay"))._make_safeguard_hook(
        "agent"
    )
    invocation = _input("Read", {"password": "prohibited-marker"})
    outcomes: list[dict[str, Any]] = []

    async def invoke() -> None:
        outcomes.append(await hook(invocation, "concurrent-id", None))

    # When an identical callback arrives while the owner is still in flight.
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(invoke)
        await runner.record_entered.wait()
        tasks.start_soon(invoke)
        await anyio.lowlevel.checkpoint()
        observed_count_before_release = runner.total_safeguard_blocks
        runner.release_record.set()

    # Then only the reserved owner applies effects and both receive the outcome.
    assert observed_count_before_release == 1
    assert runner.total_safeguard_blocks == 1
    assert len(runner.records) == 1
    assert len(outcomes) == 2
    assert outcomes[0] == outcomes[1]


@pytest.mark.anyio
async def test_persistence_exception_terminalizes_partial_effect_before_retry() -> None:
    # Given a prohibited owner whose first durable policy write raises.
    dataset = _dataset(
        {"builtin.Read": scope.ExtractorSpec(none=True)},
        prohibited={"builtin.Read": [("blocked", "prohibited-marker")]},
    )
    runner = _FailOnceRecordRunner(dataset)
    hook = _Daemon(runner, scope.ScopeStore(None, "exception-replay"))._make_safeguard_hook("agent")
    invocation = _input("Read", {"password": "prohibited-marker"})

    # When the owner raises and the provider retries the same callback.
    with pytest.raises(RuntimeError, match="injected policy persistence failure"):
        await hook(invocation, "exception-id", None)
    retry = await hook(invocation, "exception-id", None)

    # Then the retry fails closed without replaying the partial effect.
    assert _decision(retry) == "deny"
    assert "did not complete" in retry["hookSpecificOutput"]["permissionDecisionReason"]
    assert runner.total_safeguard_blocks == 1
    assert runner.record_attempts == 1
    assert len(runner.records) == 1


@pytest.mark.anyio
async def test_owner_cancellation_terminalizes_partial_effect_before_retry() -> None:
    # Given a prohibited owner paused after its counter/event mutation.
    dataset = _dataset(
        {"builtin.Read": scope.ExtractorSpec(none=True)},
        prohibited={"builtin.Read": [("blocked", "prohibited-marker")]},
    )
    runner = _BlockingRecordRunner(dataset)
    hook = _Daemon(runner, scope.ScopeStore(None, "cancel-replay"))._make_safeguard_hook("agent")
    invocation = _input("Read", {"password": "prohibited-marker"})
    owner_done = anyio.Event()
    owner_scope = anyio.CancelScope()
    owner_observed_cancellation = False

    async def invoke_owner() -> None:
        nonlocal owner_observed_cancellation
        try:
            with owner_scope:
                try:
                    await hook(invocation, "cancel-id", None)
                except anyio.get_cancelled_exc_class():
                    owner_observed_cancellation = True
                    raise
        finally:
            owner_done.set()

    # When cancellation interrupts the owner and the same callback retries.
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(invoke_owner)
        await runner.record_entered.wait()
        owner_scope.cancel()
        await owner_done.wait()
        runner.release_record.set()
    retry = await hook(invocation, "cancel-id", None)

    # Then owner cancellation propagates while retry observes terminal deny.
    assert owner_observed_cancellation is True
    assert _decision(retry) == "deny"
    assert "did not complete" in retry["hookSpecificOutput"]["permissionDecisionReason"]
    assert runner.total_safeguard_blocks == 1
    assert len(runner.records) == 1


@pytest.mark.anyio
async def test_replayed_outcome_is_isolated_from_prior_caller_mutation() -> None:
    # Given an enforce denial returned to a caller and then mutated locally.
    runner = _Runner(_dataset({}), enforce=True)
    hook = _Daemon(runner, scope.ScopeStore(None, "copy-isolation"))._make_safeguard_hook("agent")
    invocation = _input("Bash", {"command": "true"})
    first = await hook(invocation, "copy-id", None)
    first["hookSpecificOutput"]["permissionDecisionReason"] = "caller-mutated"

    # When the completed outcome is replayed.
    replay = await hook(invocation, "copy-id", None)

    # Then the cached terminal response retains its isolated original value.
    assert replay["hookSpecificOutput"]["permissionDecisionReason"] != "caller-mutated"
