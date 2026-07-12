from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import anyio
import pytest

from salient_core.daemon._runner_factory import _RunnerFactoryMixin
from salient_core.policy import scope
from salient_core.policy.defaults import DEFAULT_DATASET
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguards import SafeguardConfig


class _Runner:
    def __init__(
        self,
        dataset: PolicyDataset,
        *,
        enabled: tuple[str, ...] = (),
        enforce: bool = False,
    ) -> None:
        self.cfg: dict[str, Any] = {"enforce_builtin_policy": enforce}
        self._policy_dataset = dataset
        self._safeguard_config = SafeguardConfig()
        self._enforce_builtin_policy = enforce
        self._legacy_trusted_builtin_warned: set[str] = set()
        self._legacy_trusted_builtin_warning_lock = anyio.Lock()
        self.total_safeguard_blocks = 0
        self.options = SimpleNamespace(tools=list(enabled))
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def _record_jsonl(self, event: str, payload: dict[str, Any]) -> None:
        json.dumps(payload)
        self.records.append((event, payload))


class _Daemon(_RunnerFactoryMixin):
    def __init__(self, runner: _Runner, store: scope.ScopeStore) -> None:
        self.runners = {"agent": runner}
        self.scope = store
        self.profile: dict[str, Any] = {}


class _ControlledWarningRunner(_Runner):
    def __init__(self, dataset: PolicyDataset) -> None:
        super().__init__(dataset, enabled=("Bash",))
        self.warning_behavior: Literal["succeed", "raise", "block_then_raise"] = "succeed"
        self.warning_attempts = 0
        self.warning_entered = anyio.Event()
        self.warning_release = anyio.Event()

    async def _record_jsonl(self, event: str, payload: dict[str, Any]) -> None:
        if event == "legacy_trusted_builtin":
            self.warning_attempts += 1
            self.warning_entered.set()
            match self.warning_behavior:
                case "succeed":
                    pass
                case "raise":
                    raise OSError("simulated warning persistence failure")
                case "block_then_raise":
                    await self.warning_release.wait()
                    raise OSError("simulated concurrent warning persistence failure")
        await super()._record_jsonl(event, payload)


def _dataset(*, trusted: frozenset[str] = frozenset()) -> PolicyDataset:
    return PolicyDataset(
        tool_targets={},
        prohibited_patterns={},
        loud_patterns={},
        trusted_builtins=trusted,
    )


def _input(tool: str) -> dict[str, Any]:
    return {
        "tool_name": tool,
        "tool_input": {"auth": {"password": "raw-secret"}},
    }


def _decision(result: dict[str, Any]) -> str | None:
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


def test_default_dataset_pins_only_known_qualified_sdk_classifications() -> None:
    # Given the kernel default policy dataset.
    expected = {
        "builtin.Bash": scope.ExtractorSpec(fields={"command": "raw_argv"}),
        "builtin.Read": scope.ExtractorSpec(local_only=True),
        "builtin.Grep": scope.ExtractorSpec(local_only=True),
        "builtin.Glob": scope.ExtractorSpec(local_only=True),
        "builtin.Write": scope.ExtractorSpec(local_only=True),
        "builtin.Edit": scope.ExtractorSpec(local_only=True),
        "builtin.Agent": scope.ExtractorSpec(none=True),
        "builtin.Task": scope.ExtractorSpec(none=True),
    }

    # When its SDK-native classifications are selected.
    actual = {
        name: spec
        for name, spec in DEFAULT_DATASET.tool_targets.items()
        if name.startswith("builtin.")
    }

    # Then every known schema is explicit and exact, with no blanket entries.
    assert actual == expected


@pytest.mark.parametrize(
    "tool",
    ["TodoWrite", "ExitPlanMode", "WebSearch", "FutureTool"],
)
def test_default_dataset_leaves_unknown_sdk_tools_unclassified(tool: str) -> None:
    # Given an SDK-fired or future tool without a kernel-owned schema.
    # When the qualified default registry is inspected.
    # Then it remains absent so policy fails closed until an explicit opt-in.
    assert f"builtin.{tool}" not in DEFAULT_DATASET.tool_targets


def test_trusted_builtins_field_is_introspectably_deprecated() -> None:
    # Given callers may still construct datasets with the legacy field.
    dataset = _dataset(trusted=frozenset({"Bash"}))

    # When its public dataclass field metadata is inspected.
    field = dataset.__dataclass_fields__["trusted_builtins"]

    # Then the value remains accepted while migration guidance is observable.
    assert dataset.trusted_builtins == frozenset({"Bash"})
    assert "deprecated" in field.metadata
    assert "tool_targets" in field.metadata["deprecated"]


def test_policy_docs_do_not_advertise_empty_defaults_or_legacy_authorization() -> None:
    # Given the public architecture and extraction migration documentation.
    docs_root = Path(__file__).parents[1] / "docs"
    architecture = (docs_root / "ARCHITECTURE.md").read_text()
    extraction = (docs_root / "EXTRACTION.md").read_text()

    # When policy default and legacy authorization claims are inspected.
    combined = architecture + extraction

    # Then defaults are explicit and neither deprecated API is an enforce grant.
    assert "empty scope/safeguard dataset" not in architecture
    assert "classify_builtin" not in combined
    assert "trusted set" not in combined
    assert "enforce mode ignores" in combined.lower()


@pytest.mark.anyio
async def test_enabled_legacy_tool_warns_once_across_repeats_and_replay(
    tmp_path: Path,
) -> None:
    # Given an enabled, legacy-listed, unclassified SDK tool in shadow mode.
    runner = _Runner(_dataset(trusted=frozenset({"Bash"})), enabled=("Bash",))
    store = scope.ScopeStore(tmp_path / "scope.db", "legacy-shadow")
    hook = _Daemon(runner, store)._make_safeguard_hook("agent")
    try:
        # When it is invoked twice and the second call is replayed once.
        first = await hook(_input("Bash"), "id-1", None)
        second = await hook(_input("Bash"), "id-2", None)
        replay = await hook(_input("Bash"), "id-2", None)

        # Then all dispatch, with exactly one redacted operator warning.
        assert first == second == replay == {}
        warnings = [
            payload for event, payload in runner.records if event == "legacy_trusted_builtin"
        ]
        assert len(warnings) == 1
        assert warnings[0]["tool"] == "Bash"
        assert warnings[0]["input"]["auth"]["password"] == "<redacted-secret>"
        assert "raw-secret" not in repr(runner.records)
    finally:
        store.close()


@pytest.mark.anyio
async def test_legacy_warning_state_is_runner_lifetime_not_process_global() -> None:
    # Given two runner hook lifetimes for the same legacy-listed tool.
    runners = [_Runner(_dataset(trusted=frozenset({"Bash"})), enabled=("Bash",)) for _ in range(2)]

    # When each runner evaluates that tool once.
    for index, runner in enumerate(runners):
        hook = _Daemon(runner, scope.ScopeStore(None, f"runner-{index}"))._make_safeguard_hook(
            "agent"
        )
        assert await hook(_input("Bash"), f"id-{index}", None) == {}

    # Then each lifetime independently emits its one operator warning.
    assert [
        sum(event == "legacy_trusted_builtin" for event, _payload in runner.records)
        for runner in runners
    ] == [1, 1]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("enabled", "enforce"),
    [((), False), (("Bash",), True), ((), True)],
    ids=["disabled-shadow", "enabled-enforce", "disabled-enforce"],
)
async def test_legacy_trust_has_no_compatibility_outside_enabled_shadow(
    enabled: tuple[str, ...],
    enforce: bool,
) -> None:
    # Given an unclassified legacy-listed tool outside enabled shadow mode.
    runner = _Runner(
        _dataset(trusted=frozenset({"Bash"})),
        enabled=enabled,
        enforce=enforce,
    )
    hook = _Daemon(runner, scope.ScopeStore(None, "legacy-bounded"))._make_safeguard_hook("agent")

    # When policy evaluates the tool.
    result = await hook(_input("Bash"), "id", None)

    # Then legacy trust is ignored; enforce denies and no legacy warning appears.
    assert _decision(result) == ("deny" if enforce else None)
    assert not any(event == "legacy_trusted_builtin" for event, _payload in runner.records)


@pytest.mark.anyio
async def test_legacy_path_cannot_bypass_safeguards() -> None:
    # Given enabled shadow compatibility plus a prohibited-input safeguard.
    dataset = PolicyDataset(
        tool_targets={},
        prohibited_patterns={"builtin.Bash": [("blocked", "raw-secret")]},
        loud_patterns={},
        trusted_builtins=frozenset({"Bash"}),
    )
    runner = _Runner(dataset, enabled=("Bash",))
    hook = _Daemon(runner, scope.ScopeStore(None, "legacy-safeguard"))._make_safeguard_hook("agent")

    # When the legacy-listed tool trips the universal safeguard first.
    result = await hook(_input("Bash"), "id", None)

    # Then it is denied without entering the legacy compatibility path.
    assert _decision(result) == "deny"
    assert [event for event, _payload in runner.records] == ["safeguard_block"]
    assert runner.total_safeguard_blocks == 1


@pytest.mark.anyio
async def test_legacy_warning_persistence_failure_retries_on_distinct_id() -> None:
    # Given the first legacy warning persistence attempt raises.
    runner = _ControlledWarningRunner(_dataset(trusted=frozenset({"Bash"})))
    runner.warning_behavior = "raise"
    hook = _Daemon(runner, scope.ScopeStore(None, "legacy-write-failure"))._make_safeguard_hook(
        "agent"
    )

    # When a later distinct tool-call ID retries after persistence recovers.
    with pytest.raises(OSError, match="simulated warning persistence failure"):
        await hook(_input("Bash"), "failed-id", None)
    runner.warning_behavior = "succeed"
    result = await hook(_input("Bash"), "retry-id", None)

    # Then ownership was not committed by the failed write and retry warns once.
    assert result == {}
    assert runner.warning_attempts == 2
    assert [event for event, _payload in runner.records].count("legacy_trusted_builtin") == 1


@pytest.mark.anyio
async def test_legacy_warning_cancellation_retries_on_distinct_id() -> None:
    # Given warning persistence is suspended inside an awaited write.
    runner = _ControlledWarningRunner(_dataset(trusted=frozenset({"Bash"})))
    runner.warning_behavior = "block_then_raise"
    hook = _Daemon(runner, scope.ScopeStore(None, "legacy-write-cancel"))._make_safeguard_hook(
        "agent"
    )

    # When that invocation is cancelled and a later distinct ID retries.
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(hook, _input("Bash"), "cancelled-id", None)
        await runner.warning_entered.wait()
        task_group.cancel_scope.cancel()
    runner.warning_behavior = "succeed"
    result = await hook(_input("Bash"), "retry-id", None)

    # Then cancellation did not commit warning ownership.
    assert result == {}
    assert runner.warning_attempts == 2
    assert [event for event, _payload in runner.records].count("legacy_trusted_builtin") == 1


@pytest.mark.anyio
async def test_concurrent_legacy_calls_retry_failed_warning_without_duplicate() -> None:
    # Given two distinct IDs overlap while the first warning write will fail.
    runner = _ControlledWarningRunner(_dataset(trusted=frozenset({"Bash"})))
    runner.warning_behavior = "block_then_raise"
    hook = _Daemon(runner, scope.ScopeStore(None, "legacy-write-concurrent"))._make_safeguard_hook(
        "agent"
    )
    outcomes: dict[str, str] = {}
    second_started = anyio.Event()

    async def invoke(invocation_id: str, announce: bool = False) -> None:
        if announce:
            second_started.set()
        try:
            await hook(_input("Bash"), invocation_id, None)
            outcomes[invocation_id] = "dispatch"
        except OSError:
            outcomes[invocation_id] = "persistence-error"

    # When the second call starts before the first persistence attempt fails.
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(invoke, "first-id")
        await runner.warning_entered.wait()
        task_group.start_soon(invoke, "second-id", True)
        await second_started.wait()
        runner.warning_behavior = "succeed"
        runner.warning_release.set()

    # Then one caller retries under serialized ownership and writes exactly once.
    assert outcomes == {"first-id": "persistence-error", "second-id": "dispatch"}
    assert runner.warning_attempts == 2
    assert [event for event, _payload in runner.records].count("legacy_trusted_builtin") == 1
