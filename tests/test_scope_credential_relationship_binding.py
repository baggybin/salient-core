from __future__ import annotations

import json
from pathlib import Path

import pytest

from salient_core.policy import scope
from salient_core.policy.scope_evaluation import ScopeEvaluationKind
from tests.scope_relationship_fixtures import (
    compound_extractor,
    evaluate,
    external_spec,
    request,
)


def _store(tmp_path: Path) -> scope.ScopeStore:
    store = scope.ScopeStore(tmp_path / "scope.db", "credential-relationship")
    store.load_engagement_rules(
        {
            "scope": {
                "in_targets": [
                    "aws.example",
                    "saas:aws/username/profile-a",
                    "cloud:aws/aws/s3/us-east-1/111111111111/bucket-a",
                    "cloud:aws/aws/s3/us-east-1/333333333333/bucket-c",
                ],
                "credential_bindings": {
                    "sel": {
                        "provider": "aws",
                        "principal": "trusted-role",
                        "tenant": "tenant-a",
                        "configuration_id": "config-7",
                    }
                },
            }
        }
    )
    return store


def _bound_request(**changes: str) -> dict[str, object]:
    raw = request()
    raw["request"].update(
        {
            "credential_binding_id": "sel",
            "credential_configuration_id": "config-7",
            "principal_identity": "trusted-role",
            **changes,
        }
    )
    return raw


def test_relationship_principal_must_match_pinned_credential_binding(tmp_path: Path) -> None:
    # Given: all targets are in scope but the extractor claims another principal.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = _store(tmp_path)

    try:
        # When: the relationship is evaluated against the persisted snapshot binding.
        result = evaluate(
            external_spec(credential_binding_required=True),
            _bound_request(
                principal_identity="claimed-role",
                resource="cloud:aws/aws/s3/us-east-1/333333333333/bucket-c",
                resource_tenant="tenant-c",
            ),
            store,
        )

        # Then: the credential relationship denies before dispatch.
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED
        assert "principal" in result.reason
        assert result.snapshot_id == store.snapshot.snapshot_id
    finally:
        scope.unregister_all_extractors()


def test_valid_snapshot_binding_allows_and_selector_failures_deny(tmp_path: Path) -> None:
    # Given: one exact credential binding and independently allowed targets.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = _store(tmp_path)

    try:
        # When: exact, missing, unknown, and stale binding claims are evaluated.
        spec = external_spec(credential_binding_required=True)
        valid = evaluate(spec, _bound_request(), store, mode="probe")
        missing = evaluate(
            spec,
            _bound_request(credential_binding_id=""),
            store,
            mode="probe",
        )
        unknown = evaluate(
            spec,
            _bound_request(credential_binding_id="other"),
            store,
            mode="probe",
        )
        stale = evaluate(
            spec,
            _bound_request(credential_configuration_id="config-6"),
            store,
            mode="probe",
        )

        # Then: only the exact snapshot-bound relationship is authorized.
        assert valid.allowed is True
        assert all(result.allowed is False for result in (missing, unknown, stale))
        assert missing.kind is ScopeEvaluationKind.EXTRACTION_DENIED
        assert all(
            result.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED for result in (unknown, stale)
        )
    finally:
        scope.unregister_all_extractors()


@pytest.mark.parametrize(
    ("claim", "value", "reason"),
    [
        ("principal_provider", "azure", "provider"),
        ("principal_identity", "claimed-role", "principal"),
        ("principal_tenant", "tenant-b", "tenant"),
    ],
)
def test_snapshot_binding_rejects_each_forged_identity(
    tmp_path: Path,
    claim: str,
    value: str,
    reason: str,
) -> None:
    # Given: a required credential relationship and exact persisted binding.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = _store(tmp_path)

    try:
        # When: one extractor-emitted identity differs from the snapshot metadata.
        result = evaluate(
            external_spec(credential_binding_required=True),
            _bound_request(**{claim: value}),
            store,
            mode="probe",
        )

        # Then: the exact mismatch is a relationship denial.
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED
        assert reason in result.reason
    finally:
        scope.unregister_all_extractors()


def test_noncredential_relationship_ignores_unrelated_snapshot_bindings(tmp_path: Path) -> None:
    # Given: a legacy relationship contract and unrelated snapshot credential metadata.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = _store(tmp_path)

    try:
        # When: the relationship emits no credential binding facts.
        result = evaluate(external_spec(), request(), store, mode="probe")

        # Then: its established noncredential authorization behavior is preserved.
        assert result.allowed is True
        assert result.kind is ScopeEvaluationKind.STRICT
    finally:
        scope.unregister_all_extractors()


def test_enforce_uses_transactionally_pinned_binding_after_snapshot_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: extraction completed for a binding that rotates before authorization begins.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = _store(tmp_path)
    original = store.authorize_and_log

    def publish_then_authorize(*args, **kwargs):
        store.load_engagement_rules(
            {
                "scope": {
                    "in_targets": [
                        "aws.example",
                        "saas:aws/username/profile-a",
                        "cloud:aws/aws/s3/us-east-1/111111111111/bucket-a",
                    ],
                    "credential_bindings": {
                        "sel": {
                            "provider": "aws",
                            "principal": "rotated-role",
                            "tenant": "tenant-a",
                            "configuration_id": "config-8",
                        }
                    },
                }
            }
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "authorize_and_log", publish_then_authorize)

    try:
        # When: enforce reaches the transactional authorization operation.
        result = evaluate(
            external_spec(credential_binding_required=True),
            _bound_request(),
            store,
        )

        # Then: the new complete snapshot denies; no earlier generation authenticates the facts.
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED
        assert result.snapshot_id == store.snapshot.snapshot_id
        assert result.snapshot_generation == store.snapshot.generation
    finally:
        scope.unregister_all_extractors()


def test_binding_denial_has_probe_enforce_parity_and_durable_evidence(tmp_path: Path) -> None:
    # Given: a forged provider claim for an otherwise valid selector.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = _store(tmp_path)
    raw = _bound_request(principal_provider="azure", resource_provider="azure")

    try:
        # When: probe and enforce evaluate the same relationship.
        spec = external_spec(credential_binding_required=True)
        probe = evaluate(spec, raw, store, mode="probe")
        enforced = evaluate(spec, raw, store, mode="enforce")

        # Then: verdict and pinned snapshot match, and enforce persists opaque evidence.
        assert (probe.allowed, probe.kind, probe.reason, probe.snapshot_id) == (
            enforced.allowed,
            enforced.kind,
            enforced.reason,
            enforced.snapshot_id,
        )
        row = store._conn.execute(
            "SELECT relationships_json,snapshot_id,generation FROM scope_decisions"
        ).fetchone()
        relationship = json.loads(row[0])[0]
        assert relationship["credential_binding_id"] == "sel"
        assert relationship["credential_configuration_id"] == "config-7"
        assert row[1:] == (enforced.snapshot_id, enforced.snapshot_generation)
    finally:
        scope.unregister_all_extractors()
