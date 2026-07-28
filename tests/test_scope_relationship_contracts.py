from __future__ import annotations

from salient_core.policy import scope
from salient_core.policy.scope_evaluation import ScopeEvaluationKind
from tests.scope_relationship_fixtures import compound_extractor, evaluate, external_spec, request


def test_legacy_empty_extraction_remains_allowed_without_external_contract() -> None:
    # Given a legacy optional extractor with no external-mode contract.
    spec = scope.ExtractorSpec(fields={"target": "host_optional"})

    # When no target is present.
    result = evaluate(spec, {}, scope.ScopeStore(None, "legacy-empty"))

    # Then the existing compatibility behavior remains characterized.
    assert result.allowed is True
    assert result.kind is ScopeEvaluationKind.EMPTY_TARGETS


def test_legacy_local_classification_skips_extraction_without_external_contract() -> None:
    # Given a legacy local-only declaration with an irrelevant malformed value.
    spec = scope.ExtractorSpec(fields={"target": "host"}, local_only=True)

    # When it is evaluated without an external-mode contract.
    result = evaluate(spec, {"target": "192.0.2.1"}, scope.ScopeStore(None, "legacy-local"))

    # Then the established local shortcut remains allowed.
    assert result.allowed is True
    assert result.kind is ScopeEvaluationKind.LOCAL_ONLY


def test_external_contract_denies_partial_emission_before_empty_allow() -> None:
    # Given a compound extractor that omits one exactly required target kind.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = scope.ScopeStore(None, "missing-kind")
    raw = request()
    raw["request"]["omit"] = ["cloud"]

    try:
        # When the external mode is evaluated.
        result = evaluate(external_spec(), raw, store)

        # Then partial emission is denied before any empty-target shortcut.
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.EXTRACTION_DENIED
        assert "missing required target kinds" in result.reason
    finally:
        scope.unregister_all_extractors()


def test_external_contract_denies_wrong_tenant_despite_independent_target_allows() -> None:
    # Given independently allowed provider, principal, and resource targets.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = scope.ScopeStore(None, "wrong-tenant")
    for target in (
        "aws.example",
        "saas:aws/username/profile-a",
        "cloud:aws/aws/s3/us-east-1/333333333333/bucket-c",
    ):
        store.add_adhoc(target, reason="independently allowed")
    raw = request(
        request={
            "provider": "aws.example",
            "principal": "saas:aws/username/profile-a",
            "principal_provider": "aws",
            "principal_tenant": "tenant-a",
            "resource": "cloud:aws/aws/s3/us-east-1/333333333333/bucket-c",
            "resource_provider": "aws",
            "resource_tenant": "tenant-c",
        }
    )

    try:
        # When the mismatched relation is evaluated.
        result = evaluate(external_spec(), raw, store)

        # Then the relationship denies before independent allowlist checks can authorize it.
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED
    finally:
        scope.unregister_all_extractors()


def test_external_contract_allows_only_the_explicit_cross_tenant_pair() -> None:
    # Given one narrow tenant-a to tenant-b grant and all emitted targets in scope.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = scope.ScopeStore(None, "cross-tenant")
    for target in (
        "aws.example",
        "saas:aws/username/profile-a",
        "cloud:aws/aws/s3/us-east-1/222222222222/bucket-b",
        "cloud:aws/aws/s3/us-east-1/333333333333/bucket-c",
    ):
        store.add_adhoc(target, reason="in scope")

    allowed_raw = request(
        request={
            "provider": "aws.example",
            "principal": "saas:aws/username/profile-a",
            "principal_provider": "aws",
            "principal_tenant": "tenant-a",
            "resource": "cloud:aws/aws/s3/us-east-1/222222222222/bucket-b",
            "resource_provider": "aws",
            "resource_tenant": "tenant-b",
        }
    )
    denied_raw = request(
        request={
            "provider": "aws.example",
            "principal": "saas:aws/username/profile-a",
            "principal_provider": "aws",
            "principal_tenant": "tenant-a",
            "resource": "cloud:aws/aws/s3/us-east-1/333333333333/bucket-c",
            "resource_provider": "aws",
            "resource_tenant": "tenant-c",
        }
    )

    try:
        # When both cross-tenant pairs are evaluated.
        granted = evaluate(external_spec(), allowed_raw, store)
        ungranted = evaluate(external_spec(), denied_raw, store)

        # Then only the exact configured pair passes the relationship gate.
        assert granted.allowed is True
        assert ungranted.allowed is False
        assert ungranted.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED
    finally:
        scope.unregister_all_extractors()
