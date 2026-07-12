from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from .decision import FrozenInput, FrozenValue, ToolInvocation

if TYPE_CHECKING:
    from .scope import CheckResult, Target

_REDACTED: Final = "<redacted-secret>"


@dataclass(frozen=True, slots=True)
class ScopeAudit:
    targets: tuple[Target, ...]
    result: CheckResult


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
    return current == _REDACTED


def scope_audit(
    invocation: ToolInvocation,
    targets: tuple[Target, ...],
    result: CheckResult,
) -> ScopeAudit:
    secrets = tuple(
        dict.fromkeys(_secret_text(invocation.evaluation_input, invocation.audit_input))
    )
    if not secrets:
        return ScopeAudit(targets, result)
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
    summary = _apply_replacements(_redact_text(result.summary, secrets), replacements)
    audit_result = replace(
        result,
        decisions=[],
        summary=summary,
    )
    return ScopeAudit(audit_targets, audit_result)


__all__ = ["ScopeAudit", "scope_audit"]
