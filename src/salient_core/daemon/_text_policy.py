from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..policy.decision import FrozenInput, FrozenValue, ToolInvocation
from ..policy.registry import PolicyDataset
from ..policy.safeguard_evaluation import (
    SafeguardEvaluationRequest,
    evaluate_safeguards,
)
from ..policy.safeguards import SafeguardConfig
from ..policy.scope import ScopeStore
from ..policy.scope_evaluation import evaluate_scope


def _thaw(value: FrozenValue) -> Any:
    match value:
        case Mapping():
            return {str(key): _thaw(nested) for key, nested in value.items()}
        case tuple():
            return [_thaw(item) for item in value]
        case str() | int() | float() | bool() | None:
            return value


def thaw_input(value: FrozenInput) -> dict[str, Any]:
    return {key: _thaw(nested) for key, nested in value.items()}


@dataclass(frozen=True, slots=True)
class TextAuthorization:
    policy_allowed: bool
    dispatch_allowed: bool
    event: str
    payload: dict[str, Any]
    reason: str
    counter_delta: int = 0


async def authorize_text(
    invocation: ToolInvocation,
    *,
    dataset: PolicyDataset,
    safeguards: SafeguardConfig,
    safeguard_count: int,
    scope_store: ScopeStore | None,
    enforce: bool,
) -> TextAuthorization:
    safeguard = evaluate_safeguards(
        SafeguardEvaluationRequest(
            invocation=invocation,
            config=safeguards,
            current_strike_count=safeguard_count,
            halt_threshold=safeguards.halt_threshold,
            dataset=dataset,
        )
    )
    if not safeguard.allowed:
        assert safeguard.audit is not None
        audit = safeguard.audit
        return TextAuthorization(
            policy_allowed=False,
            dispatch_allowed=False,
            event=audit.event.value,
            payload={
                "agent": audit.agent_id,
                "tool": audit.wire_name,
                "qualified": audit.qualified_name,
                "transport": invocation.transport.value,
                "input": thaw_input(audit.audit_input),
                "reason": audit.reason,
                "count": audit.count,
                "halt_at": audit.halt_at,
                "posture": audit.posture,
            },
            reason=safeguard.model_reason,
            counter_delta=safeguard.counter_delta,
        )

    if scope_store is None:
        # No scope store configured for this runner => the scope-classification
        # layer has no opinion, mirroring the external-MCP path (its scope hook
        # is only installed when a store exists). Safeguards already hard-ran
        # above, so this is a clean allow, not a fabricated fail-closed record.
        scope_allowed = True
        policy_class = "scope_not_configured"
        reason = "scope gating not configured — no scope opinion"
    else:
        try:
            scope = await evaluate_scope(invocation, scope_store, dataset)
        except Exception as exc:
            # A configured scope store that ERRORS is an outage, not a policy
            # verdict to shadow-test. Fail closed HARD (deny dispatch even in
            # shadow, like safeguards) so no side effect runs while the store
            # that would authorize and record it is broken. The error is
            # surfaced loudly in the record rather than silently allowed.
            reason = f"scope store error — refusing fail-closed: {exc}"
            return TextAuthorization(
                policy_allowed=False,
                dispatch_allowed=False,
                event="text_policy_deny",
                payload={
                    "agent": invocation.agent_id,
                    "tool": invocation.wire_name,
                    "qualified": invocation.qualified_name,
                    "transport": invocation.transport.value,
                    "policy_class": "scope_store_error",
                    "enforce": enforce,
                    "input": thaw_input(invocation.audit_input),
                    "reason": reason,
                },
                reason=reason,
            )
        scope_allowed = scope.allowed
        policy_class = scope.kind.value
        reason = scope.reason

    event = "text_policy_allow"
    if not scope_allowed:
        event = "text_policy_deny" if enforce else "text_policy_shadow"
    return TextAuthorization(
        policy_allowed=scope_allowed,
        dispatch_allowed=scope_allowed or not enforce,
        event=event,
        payload={
            "agent": invocation.agent_id,
            "tool": invocation.wire_name,
            "qualified": invocation.qualified_name,
            "transport": invocation.transport.value,
            "policy_class": policy_class,
            "enforce": enforce,
            "input": thaw_input(invocation.audit_input),
            "reason": reason,
        },
        reason=reason,
    )


__all__ = ["TextAuthorization", "authorize_text", "thaw_input"]
