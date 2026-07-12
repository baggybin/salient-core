from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256

import anyio

from ..alias import to_real
from ..policy.decision import (
    FrozenInput,
    FrozenValue,
    InputValue,
    ToolInvocation,
    mcp_identity,
    sdk_identity,
)
from ..policy.safeguard_evaluation import SafeguardAuditDescription


@dataclass(frozen=True, slots=True)
class InvocationFingerprint:
    digest: str


@dataclass(frozen=True, slots=True)
class ToolUseIdCollisionError(ValueError):
    tool_use_id: str

    def __str__(self) -> str:
        return (
            f"tool_use_id collision for {self.tool_use_id!r}: the provider reused "
            "a completed tool-call ID for a different invocation; refusing fail-closed"
        )


class _ReplayEntry:
    """Mutable once from reserved to one terminal outcome before waking waiters."""

    __slots__ = ("fingerprint", "outcome", "ready")

    def __init__(self, fingerprint: InvocationFingerprint) -> None:
        self.fingerprint = fingerprint
        self.outcome: dict[str, InputValue] | None = None
        self.ready = anyio.Event()


@dataclass(frozen=True, slots=True)
class ReplayOwner:
    tool_use_id: str
    entry: _ReplayEntry | None


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    outcome: dict[str, InputValue]


@dataclass(frozen=True, slots=True)
class ReplayRejected:
    reason: str


ReplayReservation = ReplayOwner | ReplayOutcome | ReplayRejected


class HookReplayCache:
    """Own bounded in-flight and terminal replay state for one hook closure."""

    DEFAULT_CAPACITY = 10_000

    __slots__ = ("_capacity", "_completed")

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._completed: dict[str, _ReplayEntry] = {}

    async def reserve(
        self,
        tool_use_id: InputValue,
        invocation: ToolInvocation,
    ) -> ReplayReservation:
        match tool_use_id:
            case str() as invocation_id if invocation_id.strip():
                pass
            case _:
                return ReplayRejected(
                    "invalid tool_use_id: expected a nonempty string; refusing "
                    "fail-closed before policy effects"
                )
        fingerprint = invocation_fingerprint(invocation)
        entry = self._completed.get(invocation_id)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                return ReplayRejected(str(ToolUseIdCollisionError(invocation_id)))
            await entry.ready.wait()
            if entry.outcome is None:
                return ReplayRejected(
                    f"policy hook replay state for {invocation_id!r} ended without "
                    "an outcome; refusing fail-closed"
                )
            return ReplayOutcome(deepcopy(entry.outcome))
        if len(self._completed) >= self._capacity:
            return ReplayRejected(
                "policy hook replay capacity reached; refusing new tool-call IDs "
                "fail-closed while retaining prior replay ownership"
            )
        entry = _ReplayEntry(fingerprint)
        self._completed[invocation_id] = entry
        return ReplayOwner(tool_use_id=invocation_id, entry=entry)

    def complete(
        self,
        owner: ReplayOwner,
        outcome: dict[str, InputValue],
    ) -> dict[str, InputValue]:
        if owner.entry is not None:
            owner.entry.outcome = deepcopy(outcome)
            owner.entry.ready.set()
        return outcome

    def fail(self, owner: ReplayOwner) -> None:
        entry = owner.entry
        if entry is None or entry.ready.is_set():
            return
        entry.outcome = deny(
            f"policy hook for tool_use_id {owner.tool_use_id!r} did not complete; "
            "retry refused fail-closed to prevent duplicate policy effects"
        )
        entry.ready.set()


def normalize_sdk(
    tool_name: str, tool_input: Mapping[str, InputValue], agent: str
) -> ToolInvocation:
    return ToolInvocation.normalize(sdk_identity(tool_name, agent), tool_input)


def normalize_mcp(
    tool_name: str, tool_input: Mapping[str, InputValue], agent: str
) -> ToolInvocation:
    server = tool_name.removeprefix("mcp__").split("__", 1)[0]
    return ToolInvocation.normalize(
        mcp_identity(tool_name, agent, server_aliases={server: to_real(server)}),
        tool_input,
    )


def _thaw(value: FrozenValue) -> InputValue:
    match value:
        case Mapping():
            return {str(key): _thaw(nested) for key, nested in value.items()}
        case tuple():
            return [_thaw(item) for item in value]
        case str() | int() | float() | bool() | None:
            return value


def thaw_input(value: FrozenInput) -> dict[str, InputValue]:
    return {key: _thaw(nested) for key, nested in value.items()}


def invocation_fingerprint(invocation: ToolInvocation) -> InvocationFingerprint:
    canonical = json.dumps(
        {
            "transport": invocation.transport.value,
            "wire_name": invocation.wire_name,
            "qualified_name": invocation.qualified_name,
            "agent_id": invocation.agent_id,
            "evaluation_input": thaw_input(invocation.evaluation_input),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return InvocationFingerprint(digest=sha256(canonical.encode()).hexdigest())


def safeguard_payload(audit: SafeguardAuditDescription) -> dict[str, InputValue]:
    return {
        "agent": audit.agent_id,
        "tool": audit.wire_name,
        "qualified": audit.qualified_name,
        "input": thaw_input(audit.audit_input),
        "reason": audit.reason,
        "count": audit.count,
        "halt_at": audit.halt_at,
        "posture": audit.posture,
    }


def deny(reason: str) -> dict[str, InputValue]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def allow() -> dict[str, InputValue]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }
