"""Pure, transport-neutral safeguard and sticky-halt evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypeAlias

from .decision import FrozenInput, ToolInvocation
from .safeguards import SafeguardConfig, check_intent, check_posture

if TYPE_CHECKING:
    from .registry import PolicyDataset

CounterDelta: TypeAlias = Literal[0, 1]


class SafeguardEvent(StrEnum):
    """Durable event selected by a denied safeguard evaluation."""

    HALT_BLOCKED = "safeguard_halt_blocked"
    POSTURE_GATE = "safeguard_posture_gate"
    BLOCK = "safeguard_block"


@dataclass(frozen=True, slots=True)
class SafeguardStateError(ValueError):
    """A safeguard counter or threshold is outside its resolved domain."""

    current_strike_count: int
    halt_threshold: int

    def __str__(self) -> str:
        return (
            "invalid safeguard state: current_strike_count must be non-negative "
            "and halt_threshold must be positive"
        )


@dataclass(frozen=True, slots=True)
class SafeguardEvaluationRequest:
    """Immutable grouping of policy inputs supplied by a transport adapter."""

    invocation: ToolInvocation
    config: SafeguardConfig
    current_strike_count: int
    halt_threshold: int
    dataset: PolicyDataset

    def __post_init__(self) -> None:
        if self.current_strike_count < 0 or self.halt_threshold < 1:
            raise SafeguardStateError(
                current_strike_count=self.current_strike_count,
                halt_threshold=self.halt_threshold,
            )


@dataclass(frozen=True, slots=True)
class SafeguardAuditDescription:
    """Redacted data for the adapter's single durable denial event."""

    event: SafeguardEvent
    agent_id: str
    wire_name: str
    qualified_name: str
    audit_input: FrozenInput
    reason: str
    count: int
    halt_at: int
    posture: str


@dataclass(frozen=True, slots=True)
class SafeguardEvaluation:
    """Neutral safeguard verdict; adapters apply its effects exactly once."""

    allowed: bool
    model_reason: str
    audit: SafeguardAuditDescription | None
    counter_delta: CounterDelta


def _audit_description(
    request: SafeguardEvaluationRequest,
    event: SafeguardEvent,
    reason: str,
    count: int,
) -> SafeguardAuditDescription:
    invocation = request.invocation
    return SafeguardAuditDescription(
        event=event,
        agent_id=invocation.agent_id,
        wire_name=invocation.wire_name,
        qualified_name=invocation.qualified_name,
        audit_input=invocation.audit_input,
        reason=reason,
        count=count,
        halt_at=request.halt_threshold,
        posture=request.config.posture,
    )


def evaluate_safeguards(request: SafeguardEvaluationRequest) -> SafeguardEvaluation:
    """Evaluate sticky halt, prohibited intent, then operational posture."""
    invocation = request.invocation
    count = request.current_strike_count
    halt_at = request.halt_threshold

    if count >= halt_at:
        return SafeguardEvaluation(
            allowed=False,
            model_reason=(
                f"ENGAGEMENT HALT for {invocation.agent_id!r}: "
                f"{count}/{halt_at} safeguard blocks. Operator must "
                f"`reset {invocation.agent_id}` (or adjust the engagement "
                "profile's safeguards section) to clear. Operator-side "
                "details are logged."
            ),
            audit=_audit_description(
                request,
                SafeguardEvent.HALT_BLOCKED,
                "engagement-halted",
                count,
            ),
            counter_delta=0,
        )

    intent_allowed, intent_reason = check_intent(
        invocation.qualified_name,
        invocation.evaluation_input,
        config=request.config,
        dataset=request.dataset,
    )
    if not intent_allowed:
        new_count = count + 1
        if new_count >= halt_at:
            model_reason = (
                f"ENGAGEMENT HALT for {invocation.agent_id!r}: "
                f"{new_count}/{halt_at} safeguard blocks on "
                f"{invocation.qualified_name!r}. Operator must "
                f"`reset {invocation.agent_id}` (or adjust the engagement "
                "profile's safeguards section) to clear. Operator-side "
                "details are logged."
            )
        else:
            model_reason = (
                "safeguard refused: tool call on "
                f"{invocation.qualified_name!r} matched a policy pattern the "
                f"engagement profile blocks. Strike {new_count}/{halt_at} — "
                "agent halts at limit. Try a different approach that stays "
                "inside the engagement's authorized scope. Operator-side "
                "details are logged."
            )
        return SafeguardEvaluation(
            allowed=False,
            model_reason=model_reason,
            audit=_audit_description(
                request,
                SafeguardEvent.BLOCK,
                intent_reason,
                new_count,
            ),
            counter_delta=1,
        )

    posture_allowed, posture_reason = check_posture(
        invocation.qualified_name,
        invocation.evaluation_input,
        posture=request.config.posture,
        dataset=request.dataset,
    )
    if not posture_allowed:
        return SafeguardEvaluation(
            allowed=False,
            model_reason=(
                "posture gate: this engagement runs at the "
                f"{request.config.posture!r} posture, which holds back "
                "higher-impact options on this tool. Use the lighter-touch "
                "approach for now, or ask the operator to authorize this step "
                "(or raise the engagement posture) and retry. Operator-side "
                "details are logged."
            ),
            audit=_audit_description(
                request,
                SafeguardEvent.POSTURE_GATE,
                posture_reason,
                count,
            ),
            counter_delta=0,
        )

    return SafeguardEvaluation(
        allowed=True,
        model_reason="",
        audit=None,
        counter_delta=0,
    )
