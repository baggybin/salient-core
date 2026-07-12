from __future__ import annotations

from collections import UserDict
from dataclasses import fields
from types import MappingProxyType

import pytest

from salient_core.policy.decision import (
    ToolInvocation,
    mcp_identity,
    sdk_identity,
    text_identity,
)
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguard_evaluation import (
    SafeguardEvaluation,
    SafeguardEvaluationRequest,
    SafeguardEvent,
    evaluate_safeguards,
)
from salient_core.policy.safeguards import SafeguardConfig


def _dataset() -> PolicyDataset:
    return PolicyDataset(
        tool_targets={},
        prohibited_patterns={
            "builtin.Read": [("secret-marker", r"secret-prohibited-token")],
            "bus.ask_operator": [("secret-marker", r"secret-prohibited-token")],
            "alpha.scan": [("secret-marker", r"secret-prohibited-token")],
        },
        loud_patterns={
            "builtin.Read": [("high-signal", r"noisy-operation")],
        },
    )


def _evaluate(
    invocation: ToolInvocation,
    *,
    strike_count: int = 0,
    threshold: int = 3,
    posture: str = "normal",
) -> SafeguardEvaluation:
    return evaluate_safeguards(
        SafeguardEvaluationRequest(
            invocation=invocation,
            config=SafeguardConfig(posture=posture),
            current_strike_count=strike_count,
            halt_threshold=threshold,
            dataset=_dataset(),
        )
    )


def test_clean_call_allows_without_audit_or_counter_delta() -> None:
    invocation = ToolInvocation.normalize(
        sdk_identity("Read", "researcher"),
        {"file_path": "/tmp/clean"},
    )

    result = _evaluate(invocation)

    assert result.allowed is True
    assert result.counter_delta == 0
    assert result.audit is None


def test_posture_denial_has_one_redacted_event_and_no_strike() -> None:
    invocation = ToolInvocation.normalize(
        sdk_identity("Read", "researcher"),
        {"command": "noisy-operation", "password": "raw-secret"},
    )

    result = _evaluate(invocation, posture="stealth")

    assert result.allowed is False
    assert result.counter_delta == 0
    assert result.audit is not None
    assert result.audit.event is SafeguardEvent.POSTURE_GATE
    assert result.audit.reason == "high-signal"
    assert result.audit.audit_input["password"] == "<redacted-secret>"
    assert "raw-secret" not in repr(result.audit)
    assert "high-signal" not in result.model_reason


def test_prohibited_nested_raw_input_matches_once_but_audit_is_redacted() -> None:
    invocation = ToolInvocation.normalize(
        sdk_identity("Read", "researcher"),
        {"options": {"password": "secret-prohibited-token"}},
    )

    result = _evaluate(invocation, strike_count=1)

    assert result.allowed is False
    assert result.counter_delta == 1
    assert result.audit is not None
    assert result.audit.event is SafeguardEvent.BLOCK
    assert result.audit.count == 2
    assert result.audit.audit_input["options"]["password"] == "<redacted-secret>"
    assert "secret-prohibited-token" not in repr(result.audit)
    assert "secret-marker" not in result.model_reason
    assert "secret-prohibited-token" not in result.model_reason


def test_arbitrary_nested_mappings_match_raw_api_key_without_audit_leak() -> None:
    marker = "nested-api-key-prohibited-marker"
    nested = MappingProxyType({"credentials": UserDict({"api_key": marker})})
    invocation = ToolInvocation.normalize(
        sdk_identity("Read", "researcher"),
        UserDict({"options": nested}),
    )
    dataset = PolicyDataset(
        tool_targets={},
        prohibited_patterns={"builtin.Read": [("api-key-policy", marker)]},
        loud_patterns={},
    )

    result = evaluate_safeguards(
        SafeguardEvaluationRequest(
            invocation=invocation,
            config=SafeguardConfig(),
            current_strike_count=0,
            halt_threshold=3,
            dataset=dataset,
        )
    )

    assert result.allowed is False
    assert result.counter_delta == 1
    assert result.audit is not None
    assert result.audit.event is SafeguardEvent.BLOCK
    assert result.audit.audit_input["options"]["credentials"]["api_key"] == ("<redacted-secret>")
    assert all(
        marker not in repr(getattr(result.audit, field.name)) for field in fields(result.audit)
    )
    assert marker not in repr(result.audit)


@pytest.mark.parametrize(
    "invocation",
    [
        ToolInvocation.normalize(sdk_identity("Read", "researcher"), {"x": "clean"}),
        ToolInvocation.normalize(text_identity("ask_operator", "researcher"), {"x": "clean"}),
        ToolInvocation.normalize(mcp_identity("mcp__alpha__scan", "researcher"), {"x": "clean"}),
    ],
    ids=["sdk", "text", "mcp"],
)
def test_sticky_halt_denies_every_transport_without_new_strike(
    invocation: ToolInvocation,
) -> None:
    result = _evaluate(invocation, strike_count=3, threshold=3)

    assert result.allowed is False
    assert result.counter_delta == 0
    assert result.audit is not None
    assert result.audit.event is SafeguardEvent.HALT_BLOCKED
    assert result.audit.count == 3
    assert "ENGAGEMENT HALT" in result.model_reason


def test_sticky_halt_precedes_transport_specific_matching() -> None:
    invocation = ToolInvocation.normalize(
        sdk_identity("Read", "researcher"),
        {"password": "secret-prohibited-token"},
    )

    result = _evaluate(invocation, strike_count=3, threshold=3)

    assert result.counter_delta == 0
    assert result.audit is not None
    assert result.audit.event is SafeguardEvent.HALT_BLOCKED
    assert "secret-prohibited-token" not in repr(result.audit)
