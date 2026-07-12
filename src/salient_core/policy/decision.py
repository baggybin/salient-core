"""Transport-neutral policy invocation and decision contracts."""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

from .redaction import redact_secret_fields

InputValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | Mapping[str, "InputValue"]
    | list["InputValue"]
    | tuple["InputValue", ...]
)
FrozenValue: TypeAlias = (
    str | int | float | bool | None | Mapping[str, "FrozenValue"] | tuple["FrozenValue", ...]
)
FrozenInput: TypeAlias = Mapping[str, FrozenValue]


class InvocationTransport(StrEnum):
    """Adapter-assigned dispatch transport."""

    SDK = "sdk"
    MCP = "mcp"
    TEXT = "text"


class PolicyMode(StrEnum):
    """How a policy verdict affects dispatch."""

    SHADOW = "shadow"
    ENFORCE = "enforce"
    HARD = "hard"


@dataclass(frozen=True, slots=True)
class InvocationNameError(ValueError):
    """A transport wire name cannot be reduced to a canonical identity."""

    wire_name: str
    transport: InvocationTransport

    def __str__(self) -> str:
        return f"malformed {self.transport} invocation name: {self.wire_name!r}"


@dataclass(frozen=True, slots=True)
class InvocationInputError(TypeError):
    """An invocation input contains a value outside the transport contract."""

    value_type: str

    def __str__(self) -> str:
        return f"unsupported invocation input value type: {self.value_type}"


@dataclass(frozen=True, slots=True)
class InvocationIdentity:
    """Canonical identity assigned below the model by a transport adapter."""

    transport: InvocationTransport
    wire_name: str
    qualified_name: str
    agent_id: str


def sdk_identity(wire_name: str, agent_id: str) -> InvocationIdentity:
    """Canonicalize an SDK-native tool without treating exposure as trust."""
    if not wire_name:
        raise InvocationNameError(wire_name, InvocationTransport.SDK)
    return InvocationIdentity(
        transport=InvocationTransport.SDK,
        wire_name=wire_name,
        qualified_name=f"builtin.{wire_name}",
        agent_id=agent_id,
    )


def mcp_identity(
    wire_name: str,
    agent_id: str,
    *,
    server_aliases: Mapping[str, str] | None = None,
) -> InvocationIdentity:
    """Canonicalize external and per-agent bus MCP wire names."""
    prefix = "mcp__"
    if not wire_name.startswith(prefix):
        raise InvocationNameError(wire_name, InvocationTransport.MCP)
    remainder = wire_name.removeprefix(prefix)
    if remainder.startswith("bus__"):
        bus_remainder = remainder.removeprefix("bus__")
        if "__" not in bus_remainder:
            raise InvocationNameError(wire_name, InvocationTransport.MCP)
        _owner, bare_name = bus_remainder.rsplit("__", 1)
        qualified_name = f"bus.{bare_name}"
    else:
        if "__" not in remainder:
            raise InvocationNameError(wire_name, InvocationTransport.MCP)
        server_name, bare_name = remainder.split("__", 1)
        canonical_server = (server_aliases or {}).get(server_name, server_name)
        qualified_name = f"{canonical_server}.{bare_name}"
    if not bare_name:
        raise InvocationNameError(wire_name, InvocationTransport.MCP)
    return InvocationIdentity(
        transport=InvocationTransport.MCP,
        wire_name=wire_name,
        qualified_name=qualified_name,
        agent_id=agent_id,
    )


def text_identity(wire_name: str, agent_id: str) -> InvocationIdentity:
    """Canonicalize model-emitted text as a bus tool, never as claimed MCP."""
    bare_name = wire_name.rsplit("__", 1)[-1]
    if not bare_name:
        raise InvocationNameError(wire_name, InvocationTransport.TEXT)
    return InvocationIdentity(
        transport=InvocationTransport.TEXT,
        wire_name=wire_name,
        qualified_name=f"bus.{bare_name}",
        agent_id=agent_id,
    )


def _freeze(value: InputValue) -> FrozenValue:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(nested) for key, nested in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise InvocationInputError(type(value).__name__)


@dataclass(frozen=True, slots=True, init=False)
class ToolInvocation:
    """Frozen raw-evaluation and independently redacted audit snapshots."""

    identity: InvocationIdentity
    evaluation_input: FrozenInput
    audit_input: FrozenInput

    def __init__(
        self,
        identity: InvocationIdentity,
        raw_input: Mapping[str, InputValue],
    ) -> None:
        # Normalize top-level keys to str so the raw and redacted snapshots (and
        # the downstream replay fingerprint's json.dumps(sort_keys=True)) cannot
        # diverge or crash on a non-str key; nested keys are coerced by _freeze.
        normalized = {str(key): value for key, value in raw_input.items()}
        evaluation_copy = {key: _freeze(value) for key, value in normalized.items()}
        # Key the credential redactor off the canonical identity, never the
        # model-emitted wire name: otherwise a text call can name itself to
        # trigger — or suppress — cred redaction of its own audit projection.
        audit_copy = redact_secret_fields(normalized, tool=identity.qualified_name)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(
            self,
            "evaluation_input",
            MappingProxyType(evaluation_copy),
        )
        object.__setattr__(
            self,
            "audit_input",
            MappingProxyType({key: _freeze(value) for key, value in audit_copy.items()}),
        )

    @classmethod
    def normalize(
        cls,
        identity: InvocationIdentity,
        raw_input: Mapping[str, InputValue],
    ) -> ToolInvocation:
        """Snapshot caller input twice so mutation cannot cross the boundary."""
        return cls(identity, raw_input)

    @property
    def transport(self) -> InvocationTransport:
        return self.identity.transport

    @property
    def wire_name(self) -> str:
        return self.identity.wire_name

    @property
    def qualified_name(self) -> str:
        return self.identity.qualified_name

    @property
    def agent_id(self) -> str:
        return self.identity.agent_id


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Neutral verdict plus adapter effects, with no runtime SDK types."""

    policy_allowed: bool
    dispatch_allowed: bool
    mode: PolicyMode
    policy_class: str
    reason: str
    targets: tuple[str, ...] = ()
    audit_event: str | None = None
    counter_delta: int = 0

    @property
    def allow(self) -> bool:
        """Compatibility view of the policy verdict."""
        return self.policy_allowed


def classify_builtin(wire_name: str, trusted: AbstractSet[str]) -> PolicyDecision:
    """Return deprecated shadow-only dispatch compatibility, never authorization."""
    warnings.warn(
        "classify_builtin is deprecated shadow-only compatibility; use a qualified "
        "PolicyDataset.tool_targets classification for authorization",
        DeprecationWarning,
        stacklevel=2,
    )
    legacy_listed = wire_name in trusted
    if legacy_listed:
        return PolicyDecision(
            policy_allowed=False,
            dispatch_allowed=True,
            mode=PolicyMode.SHADOW,
            policy_class="legacy_trusted_builtin",
            reason=(
                f"{wire_name!r} is legacy-listed for shadow dispatch only; "
                "this is not a policy authorization"
            ),
        )
    return PolicyDecision(
        policy_allowed=False,
        dispatch_allowed=False,
        mode=PolicyMode.SHADOW,
        policy_class="deny_unclassified",
        reason=(
            f"built-in tool {wire_name!r} has no shadow compatibility listing; "
            "classify it with a qualified PolicyDataset.tool_targets entry"
        ),
    )
