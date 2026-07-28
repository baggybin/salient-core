from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from .decision import FrozenInput, FrozenValue, ToolInvocation

if TYPE_CHECKING:
    from .scope import CheckResult, PrincipalResourceRelationship, Target

_REDACTED: Final = "<redacted-secret>"


@dataclass(frozen=True, slots=True)
class ScopeAudit:
    targets: tuple[Target, ...]
    result: CheckResult
    relationships: tuple[PrincipalResourceRelationship, ...] = ()


def _leaf_text(value: FrozenValue) -> tuple[str, ...]:
    match value:
        case Mapping():
            return tuple(text for nested in value.values() for text in _leaf_text(nested))
        case tuple():
            return tuple(text for nested in value for text in _leaf_text(nested))
        case str() if value:
            return (value,)
        case str() | int() | float() | bool() | None:
            return ()


def _secret_text(raw: FrozenValue, audit: FrozenValue) -> tuple[str, ...]:
    match raw, audit:
        case _, "<redacted-secret>":
            return _leaf_text(raw)
        case Mapping(), Mapping():
            return tuple(
                text
                for key, raw_nested in raw.items()
                for text in _secret_text(raw_nested, audit.get(key))
            )
        case tuple(), tuple():
            return tuple(
                text
                for raw_nested, audit_nested in zip(raw, audit, strict=False)
                for text in _secret_text(raw_nested, audit_nested)
            )
        case _:
            return ()


def _replace_text(value: str, raw: str, audit: str) -> str:
    return re.sub(re.escape(raw), lambda _match: audit, value, flags=re.IGNORECASE)


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = _replace_text(redacted, secret, _REDACTED)
    return redacted


def relationship_secret_selector_error(
    invocation: ToolInvocation,
    relationships: tuple[PrincipalResourceRelationship, ...],
) -> str | None:
    secrets = tuple(
        dict.fromkeys(_secret_text(invocation.evaluation_input, invocation.audit_input))
    )
    for relationship in relationships:
        binding_id = relationship.credential_binding_id
        if binding_id is not None and _redact_text(binding_id, secrets) != binding_id:
            return "credential binding selector is sourced from secret input"
    return None


def _apply_replacements(
    value: str,
    replacements: tuple[tuple[str, str], ...],
) -> str:
    projected = value
    for raw, audit in replacements:
        projected = _replace_text(projected, raw, audit)
    return projected


def _source_is_redacted(source_field: str, audit_input: FrozenInput) -> bool:
    parts = tuple(
        part for part in source_field.replace("[", ".").replace("]", "").split(".") if part
    )
    current: FrozenValue = audit_input
    for part in parts:
        if current == _REDACTED:
            return True
        match current:
            case Mapping():
                if part not in current:
                    return False
                current = current[part]
            case tuple():
                if not part.isdecimal() or int(part) >= len(current):
                    return False
                current = current[int(part)]
            case str() | int() | float() | bool() | None:
                return False
    return _contains_redacted(current)


def _contains_redacted(value: FrozenValue) -> bool:
    match value:
        case "<redacted-secret>":
            return True
        case Mapping():
            return any(_contains_redacted(nested) for nested in value.values())
        case tuple():
            return any(_contains_redacted(nested) for nested in value)
        case str() | int() | float() | bool() | None:
            return False


def scope_audit(
    invocation: ToolInvocation,
    targets: tuple[Target, ...],
    result: CheckResult,
    relationships: tuple[PrincipalResourceRelationship, ...] = (),
) -> ScopeAudit:
    secrets = tuple(
        dict.fromkeys(_secret_text(invocation.evaluation_input, invocation.audit_input))
    )
    if not secrets:
        return ScopeAudit(targets, result, relationships)
    projected_targets = tuple(
        replace(
            target,
            value=(
                _REDACTED
                if _source_is_redacted(target.source_field, invocation.audit_input)
                else _redact_text(target.value, secrets)
            ),
            source_field=_redact_text(target.source_field, secrets),
        )
        for target in targets
    )
    replacements = tuple(
        sorted(
            (
                (raw.value, audit.value)
                for raw, audit in zip(targets, projected_targets, strict=True)
                if raw.value != audit.value
            ),
            key=lambda replacement: len(replacement[0]),
            reverse=True,
        )
    )
    audit_targets = tuple(
        replace(
            target,
            source_field=_apply_replacements(target.source_field, replacements),
        )
        for target in projected_targets
    )
    target_projection = dict(zip(targets, audit_targets, strict=True))
    summary = _apply_replacements(_redact_text(result.summary, secrets), replacements)
    audit_result = replace(
        result,
        decisions=[
            replace(
                decision,
                target=target_projection.get(decision.target, decision.target),
                reason=_apply_replacements(_redact_text(decision.reason, secrets), replacements),
            )
            for decision in result.decisions
        ],
        summary=summary,
    )
    audit_relationships = tuple(
        replace(
            relationship,
            provider=target_projection.get(relationship.provider, relationship.provider),
            principal=target_projection.get(relationship.principal, relationship.principal),
            resource=target_projection.get(relationship.resource, relationship.resource),
            principal_provider=(
                _REDACTED
                if _source_is_redacted(relationship.source_field, invocation.audit_input)
                else _redact_text(relationship.principal_provider, secrets)
            ),
            principal_tenant=(
                _REDACTED
                if _source_is_redacted(relationship.source_field, invocation.audit_input)
                else _redact_text(relationship.principal_tenant, secrets)
            ),
            resource_provider=(
                _REDACTED
                if _source_is_redacted(relationship.source_field, invocation.audit_input)
                else _redact_text(relationship.resource_provider, secrets)
            ),
            resource_tenant=(
                _REDACTED
                if _source_is_redacted(relationship.source_field, invocation.audit_input)
                else _redact_text(relationship.resource_tenant, secrets)
            ),
            credential_binding_id=(
                _REDACTED
                if _source_is_redacted(relationship.source_field, invocation.audit_input)
                else _redact_text(relationship.credential_binding_id, secrets)
                if relationship.credential_binding_id is not None
                else None
            ),
            credential_configuration_id=(
                _REDACTED
                if _source_is_redacted(relationship.source_field, invocation.audit_input)
                else _redact_text(relationship.credential_configuration_id, secrets)
                if relationship.credential_configuration_id is not None
                else None
            ),
            principal_identity=(
                _REDACTED
                if _source_is_redacted(relationship.source_field, invocation.audit_input)
                else _redact_text(relationship.principal_identity, secrets)
                if relationship.principal_identity is not None
                else None
            ),
        )
        for relationship in relationships
    )
    return ScopeAudit(audit_targets, audit_result, audit_relationships)


__all__ = ["ScopeAudit", "relationship_secret_selector_error", "scope_audit"]
