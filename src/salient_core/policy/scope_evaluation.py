from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from .decision import FrozenInput, FrozenValue, InputValue, ToolInvocation
from .scope_audit import ScopeAudit, relationship_secret_selector_error, scope_audit

if TYPE_CHECKING:
    from .registry import PolicyDataset
    from .scope import (
        CheckResult,
        ExtractorSpec,
        PrincipalResourceRelationship,
        ScopeStore,
        Target,
    )


class ScopeEvaluationKind(StrEnum):
    """Exhaustive scope branches independent of a transport response type."""

    UNCLASSIFIED = "unclassified"
    TARGETLESS = "targetless"
    SESSION_BYPASS = "session_bypass"
    LOCAL_ONLY = "local_only"
    EXTRACTION_DENIED = "extraction_denied"
    RELATIONSHIP_DENIED = "relationship_denied"
    EMPTY_TARGETS = "empty_targets"
    STRICT = "strict"
    RESEARCH = "research"


@dataclass(frozen=True, slots=True)
class ScopeEvaluation:
    """A scope verdict that adapters can render without importing SDK types."""

    allowed: bool
    kind: ScopeEvaluationKind
    reason: str
    targets: tuple[Target, ...]
    classification_key: str | None
    relationships: tuple[PrincipalResourceRelationship, ...] = ()
    snapshot_id: str = ""
    snapshot_generation: int = 0
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopeEvaluationRequest:
    """Adapter-selected dataset and permission to use the research lane."""

    dataset: PolicyDataset | None = None
    allow_research: bool = True


def _thaw(value: FrozenValue) -> InputValue:
    match value:
        case Mapping():
            return {str(key): _thaw(nested) for key, nested in value.items()}
        case tuple():
            return [_thaw(item) for item in value]
        case str() | int() | float() | bool() | None:
            return value


def _thaw_input(value: FrozenInput) -> dict[str, InputValue]:
    return {key: _thaw(nested) for key, nested in value.items()}


def _resolved_spec(
    invocation: ToolInvocation,
    dataset: PolicyDataset,
) -> tuple[str | None, ExtractorSpec | None]:
    qualified = invocation.qualified_name
    spec = dataset.tool_targets.get(qualified)
    if spec is not None:
        return qualified, spec
    bare = qualified.rpartition(".")[2]
    return (bare, dataset.tool_targets[bare]) if bare in dataset.tool_targets else (None, None)


def _record(
    invocation: ToolInvocation,
    store: ScopeStore,
    audit: ScopeAudit,
    correlation_id: str | None = None,
    decision_id: str | None = None,
) -> CheckResult:
    return store.log_decision(
        agent=invocation.agent_id,
        tool=invocation.wire_name,
        args=_thaw_input(invocation.audit_input),
        targets=list(audit.targets),
        relationships=list(audit.relationships),
        result=audit.result,
        correlation_id=correlation_id,
        decision_id=decision_id,
    )


async def evaluate_scope(
    invocation: ToolInvocation,
    store: ScopeStore,
    dataset: PolicyDataset | ScopeEvaluationRequest | None = None,
    *,
    mode: Literal["enforce", "probe"] = "enforce",
    correlation_id: str | None = None,
    decision_id: str | None = None,
) -> ScopeEvaluation:
    """Classify, evaluate, and durably record at most one scope decision.

    ``mode="probe"`` computes the IDENTICAL verdict but READ-ONLY: it writes no
    audit row and consumes no one-shot rule (routes the strict lane through
    ``store.dry_check`` — the read-only twin of ``store.check``). The enforce
    path is byte-for-byte unchanged. A caller previewing what the gate WOULD
    decide (the ``scope gate-probe`` RPC) passes ``mode="probe"``; the gate
    dispatch never does. Verdict parity is pinned by the enforce==probe matrix
    test in tests/ — the anti-drift guarantee that the probe cannot lie.
    """
    from . import scope
    from .registry import PolicyDataset, get_active

    def _emit(audit: ScopeAudit) -> tuple[scope.CheckResult, bool]:
        # Single choke point for the durable audit write: it happens ONLY when
        # enforcing. A new evaluate_scope branch that calls _emit cannot leak an
        # audit row into probe mode, because it never touches `mode` itself.
        if mode == "enforce":
            try:
                return _record(invocation, store, audit, correlation_id, decision_id), True
            except sqlite3.Error:
                snapshot = store.pin_snapshot()
                return (
                    scope.CheckResult(
                        allowed=False,
                        decisions=[],
                        summary="audit persistence failed; authorization denied",
                        snapshot_id=snapshot.snapshot_id,
                        snapshot_generation=snapshot.generation,
                    ),
                    False,
                )
        snapshot = store.pin_snapshot()
        return (
            replace(
                audit.result,
                snapshot_id=snapshot.snapshot_id,
                snapshot_generation=snapshot.generation,
            ),
            True,
        )

    def _finish(
        check: scope.CheckResult,
        kind: ScopeEvaluationKind,
        reason: str,
        targets: tuple[Target, ...],
        classification_key: str | None,
        relationships: tuple[PrincipalResourceRelationship, ...] = (),
    ) -> ScopeEvaluation:
        audit = scope_audit(invocation, targets, check, relationships)
        emitted, audit_ok = _emit(audit)
        return ScopeEvaluation(
            allowed=emitted.allowed,
            kind=kind,
            reason=reason if audit_ok else "audit persistence failed; authorization denied",
            targets=audit.targets,
            classification_key=classification_key,
            relationships=audit.relationships,
            snapshot_id=emitted.snapshot_id,
            snapshot_generation=emitted.snapshot_generation,
            rule_ids=emitted.rule_ids,
        )

    match dataset:
        case ScopeEvaluationRequest(dataset=request_dataset, allow_research=allow_research):
            active_dataset = request_dataset or get_active()
        case PolicyDataset():
            active_dataset = dataset
            allow_research = True
        case None:
            active_dataset = get_active()
            allow_research = True
    classification_key, spec = _resolved_spec(invocation, active_dataset)
    if spec is None:
        check = scope.CheckResult(
            allowed=False,
            decisions=[],
            summary=f"tool {invocation.wire_name!r} has no scope classification",
        )
        return _finish(
            check,
            ScopeEvaluationKind.UNCLASSIFIED,
            (
                f"tool {invocation.wire_name!r} has no scope classification — "
                "refusing fail-closed. Add an entry to the active "
                "PolicyDataset.tool_targets (policy.registry.set_active)."
            ),
            (),
            None,
        )
    evaluation_input = _thaw_input(invocation.evaluation_input)
    external_variant = None
    if spec.external_modes is not None:
        selector = evaluation_input.get(spec.external_modes.selector_field)
        if not isinstance(selector, str) or not selector:
            check = scope.CheckResult(
                allowed=False,
                decisions=[],
                summary=f"missing external mode selector {spec.external_modes.selector_field!r}",
            )
            return _finish(
                check,
                ScopeEvaluationKind.EXTRACTION_DENIED,
                f"extractor refused: {check.summary}",
                (),
                classification_key,
            )
        external_variant = spec.external_modes.resolve(selector)
        if external_variant is None:
            check = scope.CheckResult(
                allowed=False,
                decisions=[],
                summary=f"unknown external mode selector {selector!r}",
            )
            return _finish(
                check,
                ScopeEvaluationKind.EXTRACTION_DENIED,
                f"extractor refused: {check.summary}",
                (),
                classification_key,
            )
    if spec.none and external_variant is None:
        check = scope.CheckResult(
            allowed=True,
            decisions=[],
            summary="targetless tool — scope check skipped",
        )
        return _finish(
            check,
            ScopeEvaluationKind.TARGETLESS,
            check.summary,
            (),
            classification_key,
        )
    if spec.session_scoped and not store.session_strict() and external_variant is None:
        check = scope.CheckResult(
            allowed=True,
            decisions=[],
            summary="session-scoped tool — strict session checking disabled",
        )
        return _finish(
            check,
            ScopeEvaluationKind.SESSION_BYPASS,
            check.summary,
            (),
            classification_key,
        )
    if spec.local_only and external_variant is None:
        check = scope.CheckResult(
            allowed=True,
            decisions=[],
            summary="local-only tool — scope check skipped",
        )
        return _finish(
            check,
            ScopeEvaluationKind.LOCAL_ONLY,
            check.summary,
            (),
            classification_key,
        )

    try:
        extraction = scope.extract_scope(spec, evaluation_input)
    except scope.ExtractorError as error:
        check = scope.CheckResult(
            allowed=False,
            decisions=[],
            summary=f"extractor: {error}",
        )
        return _finish(
            check,
            ScopeEvaluationKind.EXTRACTION_DENIED,
            f"extractor refused: {error}",
            (),
            classification_key,
        )
    extracted = extraction.targets
    relationships = extraction.relationships
    secret_selector_error = relationship_secret_selector_error(invocation, relationships)
    if secret_selector_error is not None:
        check = scope.CheckResult(
            allowed=False,
            decisions=[],
            summary=secret_selector_error,
        )
        return _finish(
            check,
            ScopeEvaluationKind.EXTRACTION_DENIED,
            f"extractor refused: {secret_selector_error}",
            extracted,
            classification_key,
            relationships,
        )
    empty_target = next((target for target in extracted if not target.value.strip()), None)
    if empty_target is not None:
        check = scope.CheckResult(
            allowed=False,
            decisions=[],
            summary=f"empty target value emitted for kind {empty_target.kind!r}",
        )
        return _finish(
            check,
            ScopeEvaluationKind.EXTRACTION_DENIED,
            f"extractor refused: {check.summary}",
            extracted,
            classification_key,
            relationships,
        )
    if external_variant is not None:
        emitted_kinds = frozenset(target.kind for target in extracted)
        missing_kinds = external_variant.required_target_kinds - emitted_kinds
        if missing_kinds:
            check = scope.CheckResult(
                allowed=False,
                decisions=[],
                summary=f"missing required target kinds: {sorted(missing_kinds)}",
            )
            return _finish(
                check,
                ScopeEvaluationKind.EXTRACTION_DENIED,
                f"extractor refused: {check.summary}",
                extracted,
                classification_key,
                relationships,
            )
    if not extracted:
        check = scope.CheckResult(
            allowed=True,
            decisions=[],
            summary="extraction returned no targets — call allowed",
        )
        return _finish(
            check,
            ScopeEvaluationKind.EMPTY_TARGETS,
            check.summary,
            (),
            classification_key,
            relationships,
        )

    use_research = allow_research and spec.research and store.research_active()
    if use_research:
        loop = asyncio.get_running_loop()
        check = await loop.run_in_executor(
            scope._RESEARCH_EXECUTOR,
            store.check_research,
            list(extracted),
            spec.research_active,
        )
        kind = ScopeEvaluationKind.RESEARCH
    else:
        # Probe mode uses dry_check — identical verdict, never consumes a
        # one-shot rule. (The research lane's check_research is already
        # store-read-only, so it needs no probe variant.)
        if mode == "enforce":
            check = store.authorize_and_log(
                invocation,
                _thaw_input(invocation.audit_input),
                extracted,
                relationships,
                relationship_variant=external_variant,
                correlation_id=correlation_id,
                decision_id=decision_id,
            )
        else:
            check = store.dry_check(
                list(extracted),
                relationships,
                relationship_variant=external_variant,
            )
        kind = (
            ScopeEvaluationKind.RELATIONSHIP_DENIED
            if check.relationship_denied
            else ScopeEvaluationKind.STRICT
        )
    audit_view = scope_audit(invocation, extracted, check, relationships)
    extracted = audit_view.targets
    relationships = audit_view.relationships
    if check.allowed:
        if use_research:
            return _finish(
                check,
                kind,
                check.summary,
                extracted,
                classification_key,
                relationships,
            )
        return ScopeEvaluation(
            allowed=True,
            kind=kind,
            reason=check.summary,
            targets=extracted,
            classification_key=classification_key,
            relationships=relationships,
            snapshot_id=check.snapshot_id,
            snapshot_generation=check.snapshot_generation,
            rule_ids=check.rule_ids,
        )

    example = extracted[0].value
    if check.relationship_denied:
        reason = f"relationship refused: {check.summary}"
    elif use_research:
        reason = (
            f"research target refused — {check.summary}\n\n"
            "The research lane reaches any PUBLIC host but denies "
            "internal/private infrastructure and `out_targets`. To "
            "reach an internal target, add it to engagement scope: "
            f"`salientctl scope add {example} --reason '…'`."
        )
    else:
        reason = (
            f"out of scope — {check.summary}\n\n"
            "To allow: `salientctl scope add <pattern> --reason '…'` "
            "(or `--once` for single-use). "
            f"To inspect: `salientctl scope test {example}` "
            "or `salientctl scope deny-log --since 5m`."
        )
    if use_research:
        return _finish(
            check,
            kind,
            reason,
            extracted,
            classification_key,
            relationships,
        )
    return ScopeEvaluation(
        allowed=False,
        kind=kind,
        reason=reason,
        targets=extracted,
        classification_key=classification_key,
        relationships=relationships,
        snapshot_id=check.snapshot_id,
        snapshot_generation=check.snapshot_generation,
        rule_ids=check.rule_ids,
    )


__all__ = [
    "ScopeEvaluation",
    "ScopeEvaluationKind",
    "ScopeEvaluationRequest",
    "evaluate_scope",
]
