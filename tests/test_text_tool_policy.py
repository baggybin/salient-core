from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from salient_core.bus import ContextStore
from salient_core.coord.questions import QuestionInbox
from salient_core.daemon import AgentRunner, Job
from salient_core.memory.kg import KnowledgeGraph
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguards import SafeguardConfig
from salient_core.policy.scope import ExtractorSpec, ScopeStore

_CALLS = {
    "ask_operator": {"question": "May I continue?"},
    "context_write": {"key": "finding", "value": "safe value"},
    "kg_assert": {"subject": "service", "predicate": "uses", "object": "sqlite"},
    "cred_record": {"kind": "password", "user": "alice", "value": "s3cr3t"},
}


@dataclass
class _Daemon:
    context: ContextStore
    kg: KnowledgeGraph
    inbox: QuestionInbox
    engagement_path: Path

    def add_question(self, agent: str, text: str, job_id: int | None = None) -> int:
        return self.inbox.add(agent, text, job_id=job_id or 0).id


@pytest.fixture
def runner_resources(tmp_path: Path):
    context = ContextStore(tmp_path / "context.db", events_cap_per_agent=0)
    kg = KnowledgeGraph(tmp_path / "kg.db")
    inbox = QuestionInbox(context)
    scope = ScopeStore(tmp_path / "scope.db", "text-agent")
    daemon = _Daemon(context, kg, inbox, tmp_path / "engagement")
    runner = AgentRunner(
        name="text-agent",
        cfg={},
        context=context,
    )
    runner._daemon = daemon
    runner._scope_store = scope
    runner._safeguard_config = SafeguardConfig()
    try:
        yield runner, daemon
    finally:
        scope.close()
        kg.close()
        context.close()


def _dataset(*names: str, prohibited: dict[str, list[tuple[str, str]]] | None = None):
    return PolicyDataset(
        tool_targets={f"bus.{name}": ExtractorSpec(none=True) for name in names},
        prohibited_patterns=prohibited or {},
        loud_patterns={},
    )


def _job(name: str, args: dict[str, Any], *, job_id: int = 1) -> Job:
    call = f"<function={name}>{json.dumps(args)}</function>"
    return Job(id=job_id, prompt="test", submitted_at=0.0, result=call)


def _state(daemon: _Daemon) -> tuple[int, str | None, int]:
    return (
        len(daemon.inbox.questions),
        daemon.context.read("text-agent", "finding"),
        int(daemon.kg.stats()["total_facts"]),
    )


def _events(daemon: _Daemon) -> list[dict[str, Any]]:
    return daemon.context.query_events(agent="text-agent", limit=100)


@pytest.mark.anyio
@pytest.mark.parametrize(("name", "args"), _CALLS.items())
async def test_enforce_denial_precedes_every_text_mutator(
    runner_resources: tuple[AgentRunner, _Daemon],
    name: str,
    args: dict[str, Any],
) -> None:
    # Given: an enforce-mode runner whose active dataset does not classify the call.
    runner, daemon = runner_resources
    runner._policy_dataset = _dataset()
    runner._enforce_builtin_policy = True
    before = _state(daemon)

    # When: the model emits a side-effecting text call.
    await runner._dispatch_text_function_calls(_job(name, args))

    # Then: policy denial is durable and no mutator or success record ran.
    assert _state(daemon) == before
    events = _events(daemon)
    assert [event["kind"] for event in events] == ["text_policy_deny", "tool-error"]
    assert not any(event["kind"] in {"tool-call", "tool-result"} for event in events)


@pytest.mark.anyio
async def test_shadow_denial_records_once_then_dispatches(
    runner_resources: tuple[AgentRunner, _Daemon],
) -> None:
    # Given: an unclassified context write in rollout shadow mode.
    runner, daemon = runner_resources
    runner._policy_dataset = _dataset()
    runner._enforce_builtin_policy = False

    # When: the text adapter handles the call.
    await runner._dispatch_text_function_calls(_job("context_write", _CALLS["context_write"]))

    # Then: the denial is recorded once and the normal execution pair remains.
    assert daemon.context.read("text-agent", "finding") == "safe value"
    kinds = [event["kind"] for event in _events(daemon)]
    assert kinds.count("text_policy_shadow") == 1
    assert kinds.count("tool-call") == 1
    assert kinds.count("tool-result") == 1


@pytest.mark.anyio
@pytest.mark.parametrize(("name", "args"), _CALLS.items())
async def test_classified_text_call_has_one_policy_and_execution_pair(
    runner_resources: tuple[AgentRunner, _Daemon],
    name: str,
    args: dict[str, Any],
) -> None:
    # Given: an explicitly classified text mutator in enforce mode.
    runner, daemon = runner_resources
    runner._policy_dataset = _dataset(name)
    runner._enforce_builtin_policy = True

    # When: the text adapter handles the call.
    await runner._dispatch_text_function_calls(_job(name, args))

    # Then: one allow record precedes one successful execution pair.
    kinds = [event["kind"] for event in _events(daemon)]
    assert kinds == ["text_policy_allow", "tool-call", "tool-result"]


@pytest.mark.anyio
async def test_text_transport_ignores_forged_mcp_server_and_redacts_secret(
    runner_resources: tuple[AgentRunner, _Daemon],
) -> None:
    # Given: a classified credential call whose model-controlled name claims MCP identity.
    runner, daemon = runner_resources
    runner._policy_dataset = _dataset("cred_record")
    runner._enforce_builtin_policy = True
    args = {
        **_CALLS["cred_record"],
        "metadata": {"password": "nested-secret", "labels": ["public"]},
    }

    # When: the forged call is dispatched through the text adapter.
    await runner._dispatch_text_function_calls(
        _job("mcp__forged__cred_record", args),
    )

    # Then: durable records say text/bus identity and contain no raw secret.
    events = _events(daemon)
    policy = next(
        (event for event in events if event["kind"] == "text_policy_allow"),
        None,
    )
    assert policy is not None, events
    assert policy["content"]["transport"] == "text"
    assert policy["content"]["qualified"] == "bus.cred_record"
    serialized = json.dumps(events)
    assert "s3cr3t" not in serialized
    assert "nested-secret" not in serialized


@pytest.mark.anyio
async def test_unsupported_text_call_never_dispatches_or_authorizes(
    runner_resources: tuple[AgentRunner, _Daemon],
) -> None:
    # Given: a read-side call that the fallback deliberately cannot consume.
    runner, daemon = runner_resources
    runner._policy_dataset = _dataset("context_read")

    # When: the model emits it as text.
    await runner._dispatch_text_function_calls(_job("context_read", {"key": "finding"}))

    # Then: compatibility behavior remains unsupported without a policy/execution record.
    kinds = [event["kind"] for event in _events(daemon)]
    assert kinds == ["tool-fallback-unsupported"]


@pytest.mark.anyio
async def test_native_operator_question_dedup_still_skips_text_policy(
    runner_resources: tuple[AgentRunner, _Daemon],
) -> None:
    # Given: the same turn already filed a native operator question.
    runner, daemon = runner_resources
    question = daemon.inbox.add("text-agent", "native", job_id=7)
    job = _job("ask_operator", _CALLS["ask_operator"], job_id=7)
    job.tool_question_ids.append(question.id)

    # When: leftover text-call noise is post-processed.
    await runner._dispatch_text_function_calls(job)

    # Then: no second dispatch or policy record is produced.
    assert len(daemon.inbox.questions) == 1
    assert _events(daemon) == []


@pytest.mark.anyio
async def test_sticky_halt_denies_text_before_scope_and_counter_is_unchanged(
    runner_resources: tuple[AgentRunner, _Daemon],
) -> None:
    # Given: a classified call on a runner whose safeguard halt is already sticky.
    runner, daemon = runner_resources
    runner._policy_dataset = _dataset("context_write")
    runner._enforce_builtin_policy = True
    runner.total_safeguard_blocks = 3

    # When: the model emits a clean write.
    await runner._dispatch_text_function_calls(_job("context_write", _CALLS["context_write"]))

    # Then: halt wins before scope/mutation, is audited once, and adds no strike.
    assert daemon.context.read("text-agent", "finding") is None
    assert runner.total_safeguard_blocks == 3
    kinds = [event["kind"] for event in _events(daemon)]
    assert kinds == ["safeguard_halt_blocked", "tool-error"]


@pytest.mark.anyio
async def test_prohibited_text_call_applies_one_counter_delta_and_one_audit(
    runner_resources: tuple[AgentRunner, _Daemon],
) -> None:
    # Given: one prohibited raw value below the halt threshold.
    runner, daemon = runner_resources
    runner._policy_dataset = _dataset(
        "context_write",
        prohibited={"bus.context_write": [("blocked", "forbidden-value")]},
    )
    runner._enforce_builtin_policy = True
    args = {"key": "finding", "value": "forbidden-value"}

    # When: the text adapter evaluates it.
    await runner._dispatch_text_function_calls(_job("context_write", args))

    # Then: exactly one strike/event is applied and no write occurs.
    assert runner.total_safeguard_blocks == 1
    assert daemon.context.read("text-agent", "finding") is None
    kinds = [event["kind"] for event in _events(daemon)]
    assert kinds == ["safeguard_block", "tool-error"]


@pytest.mark.anyio
async def test_unconfigured_scope_store_cleanly_allows_without_false_failclosed(
    runner_resources: tuple[AgentRunner, _Daemon],
) -> None:
    # Given: a runner with NO scope store configured (scope gating off), which is
    # a legitimate configuration — the scope-classification layer has no opinion,
    # mirroring the external-MCP path whose scope hook is only installed with a
    # store. Safeguards still hard-run.
    runner, daemon = runner_resources
    runner._scope_store = None
    runner._policy_dataset = _dataset("context_write")
    runner._enforce_builtin_policy = True

    # When: a side-effecting text call is dispatched under enforce mode.
    await runner._dispatch_text_function_calls(
        _job("context_write", _CALLS["context_write"]),
    )

    # Then: it is a clean allow (the mutation runs) with an honest policy_class —
    # never a fabricated "refusing fail-closed" record that also dispatches.
    assert daemon.context.read("text-agent", "finding") == "safe value"
    events = _events(daemon)
    kinds = [event["kind"] for event in events]
    assert kinds == ["text_policy_allow", "tool-call", "tool-result"]
    policy = next(event for event in events if event["kind"] == "text_policy_allow")
    assert policy["content"]["policy_class"] == "scope_not_configured"
    assert "fail-closed" not in policy["content"]["reason"]


def test_wrong_typed_scope_store_is_rejected_at_assignment(
    runner_resources: tuple[AgentRunner, _Daemon],
) -> None:
    # A non-None value that is not a ScopeStore (stale import, mock leaking into
    # prod, refactored class) must fail loudly at the injection boundary instead
    # of being silently coerced to "unconfigured" downstream.
    runner, _ = runner_resources
    with pytest.raises(TypeError):
        runner._scope_store = object()


@pytest.mark.anyio
async def test_broken_scope_store_fails_closed_hard_even_in_shadow(
    runner_resources: tuple[AgentRunner, _Daemon],
    tmp_path: Path,
) -> None:
    # Given: a CONFIGURED scope store that errors when it records a decision —
    # an outage, not a policy verdict to shadow-test.
    runner, daemon = runner_resources

    class _BrokenScope(ScopeStore):
        def log_decision(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("scope db is on fire")

    broken = _BrokenScope(tmp_path / "broken.db", "text-agent")
    runner._scope_store = broken
    runner._policy_dataset = _dataset()  # unclassified -> reaches store.log_decision
    runner._enforce_builtin_policy = False  # SHADOW

    # When: a side-effecting text call is dispatched in shadow mode.
    try:
        await runner._dispatch_text_function_calls(
            _job("context_write", _CALLS["context_write"]),
        )
    finally:
        broken.close()

    # Then: the store outage denies dispatch HARD (no mutation) even in shadow,
    # recorded as a scope_store_error rather than silently allowed.
    assert daemon.context.read("text-agent", "finding") is None
    events = _events(daemon)
    kinds = [event["kind"] for event in events]
    assert kinds == ["text_policy_deny", "tool-error"]
    deny = next(event for event in events if event["kind"] == "text_policy_deny")
    assert deny["content"]["policy_class"] == "scope_store_error"
