from __future__ import annotations

import pytest

from salient_core.policy import scope, scope_api
from salient_core.policy.scope_evaluation import ScopeEvaluationKind
from tests.scope_relationship_fixtures import compound_extractor, evaluate, external_spec, request


def test_external_contract_denies_unknown_or_missing_mode_before_shortcuts() -> None:
    # Given contradictory local/targetless shortcuts carrying an external contract.
    store = scope.ScopeStore(None, "unknown-mode")

    # When the selector is absent or unknown for each shortcut shape.
    results = [
        evaluate(external_spec(local_only=True), request(mode="unknown"), store),
        evaluate(external_spec(none=True), {"request": request()["request"]}, store),
    ]

    # Then neither shortcut can bypass mode validation.
    assert all(result.allowed is False for result in results)
    assert all(result.kind is ScopeEvaluationKind.EXTRACTION_DENIED for result in results)


def test_relationship_contract_has_probe_enforce_parity() -> None:
    # Given a wrong-tenant compound request.
    scope.register_extractor("compound_cloud", compound_extractor)
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
        # When probe and enforce evaluate the same contract.
        probe = evaluate(external_spec(), raw, scope.ScopeStore(None, "probe"), mode="probe")
        enforce = evaluate(external_spec(), raw, scope.ScopeStore(None, "enforce"))

        # Then both modes expose the same relationship denial.
        assert (probe.allowed, probe.kind, probe.targets) == (
            enforce.allowed,
            enforce.kind,
            enforce.targets,
        )
    finally:
        scope.unregister_all_extractors()


def test_provider_mismatch_and_missing_relationship_fact_deny() -> None:
    # Given a compound extractor and an external relationship requirement.
    scope.register_extractor("compound_cloud", compound_extractor)
    mismatched = request()
    mismatched["request"]["resource_provider"] = "azure"

    def targets_only(ctx: scope.ExtractorCtx) -> scope.ExtractionResult:
        return scope.ExtractionResult(targets=compound_extractor(ctx).targets)

    try:
        # When provider-incompatible and relationship-free outputs are evaluated.
        provider_result = evaluate(
            external_spec(), mismatched, scope.ScopeStore(None, "provider-mismatch")
        )
        scope.register_extractor("compound_cloud", targets_only, override=True)
        missing_result = evaluate(
            external_spec(), request(), scope.ScopeStore(None, "missing-relationship")
        )

        # Then both malformed relationship variants fail closed.
        assert provider_result.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED
        assert missing_result.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED
    finally:
        scope.unregister_all_extractors()


def test_provider_target_identity_mismatch_denies_exact_verifier_case() -> None:
    # Given independently allowed Azure endpoint and AWS principal/resource targets.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = scope.ScopeStore(None, "provider-target-mismatch")
    raw = request()
    raw["request"]["provider"] = "azure.example"
    for target in (
        "azure.example",
        "saas:aws/username/profile-a",
        "cloud:aws/aws/s3/us-east-1/111111111111/bucket-a",
    ):
        store.add_adhoc(target, reason="independently allowed")

    try:
        # When the relationship claims AWS while emitting an Azure provider target.
        result = evaluate(external_spec(), raw, store)

        # Then target-policy binding rejects the incompatible provider identity.
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED
        assert "provider target" in result.reason
    finally:
        scope.unregister_all_extractors()


def test_cross_tenant_grant_rejects_different_principal_and_resource() -> None:
    # Given a grant for one exact principal and bucket in tenant-b.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = scope.ScopeStore(None, "narrow-grant")
    raw = request(
        request={
            "provider": "aws.example",
            "principal": "saas:aws/username/other-profile",
            "principal_provider": "aws",
            "principal_tenant": "tenant-a",
            "resource": "cloud:aws/aws/s3/us-east-1/222222222222/other-bucket",
            "resource_provider": "aws",
            "resource_tenant": "tenant-b",
        }
    )
    for target in (
        "aws.example",
        "saas:aws/username/other-profile",
        "cloud:aws/aws/s3/us-east-1/222222222222/other-bucket",
    ):
        store.add_adhoc(target, reason="independently allowed")

    try:
        # When another relationship reuses only the granted provider/tenant pair.
        result = evaluate(external_spec(), raw, store)

        # Then the exact relationship grant does not authorize different identities.
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.RELATIONSHIP_DENIED
    finally:
        scope.unregister_all_extractors()


def test_empty_emitted_target_value_is_typed_extraction_denial() -> None:
    # Given a compound extractor emitting an empty provider target value.
    scope.register_extractor("compound_cloud", compound_extractor)
    raw = request()
    raw["request"]["provider"] = ""

    try:
        # When the malformed compound output reaches the evaluator boundary.
        result = evaluate(external_spec(), raw, scope.ScopeStore(None, "empty-target"))

        # Then it is rejected as malformed extraction before strict scope matching.
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.EXTRACTION_DENIED
        assert "empty target value" in result.reason
    finally:
        scope.unregister_all_extractors()


def test_external_mode_contract_rejects_empty_or_duplicate_variants() -> None:
    # Given malformed external contract shapes.
    valid_contract = external_spec().external_modes
    assert valid_contract is not None
    variant = valid_contract.variants[0]

    # When each malformed shape is constructed, then policy parsing fails early.
    with pytest.raises(scope.ExternalModeContractError):
        scope.ExternalModeContract(selector_field="mode", variants=())
    with pytest.raises(scope.ExternalModeContractError):
        scope.ExternalModeContract(selector_field="mode", variants=(variant, variant))
    with pytest.raises(scope.ExternalModeContractError):
        scope.ExternalModeVariant(selector="empty", required_target_kinds=frozenset())


def test_scope_api_version_bump_and_stale_skin_failure() -> None:
    # Given the compound extraction API release.
    assert scope_api.SCOPE_API_VERSION == 3

    # When a stale skin asserts the prior API, then startup fails explicitly.
    with pytest.raises(scope_api.ScopeApiVersionError, match="scope API mismatch"):
        scope_api.require_scope_api_version(2)
    scope_api.require_scope_api_version(3)


def test_scope_api_facade_exports_the_evaluation_entry_point() -> None:
    """The facade's contract is "the kernel refactors internals freely as long
    as this surface holds" — so a name a skin depends on has to BE on the
    surface. `salient-assay`'s redirect floor re-judges every HTTP hop by
    calling the kernel's own evaluator; before these were promoted it reached
    into `policy.decision` / `policy.scope_evaluation` directly, and a rename
    there would have broken a skin's floor without any version signal.
    """
    for name in (
        "evaluate_scope",
        "ToolInvocation",
        "InvocationIdentity",
        "InvocationTransport",
        "ScopeEvaluation",
        "ScopeEvaluationKind",
    ):
        assert name in scope_api.__all__, f"{name} dropped out of the facade"
        assert getattr(scope_api, name, None) is not None

    # Same objects as the internals — a facade, not a reimplementation.
    from salient_core.policy import decision, scope_evaluation

    assert scope_api.evaluate_scope is scope_evaluation.evaluate_scope
    assert scope_api.ToolInvocation is decision.ToolInvocation
    assert scope_api.InvocationIdentity is decision.InvocationIdentity


def test_scope_api_facade_promotion_was_additive() -> None:
    """Promoting evaluate_scope must not have moved the version: the check is
    exact equality, so a bump breaks every pinned skin. Removing or changing
    a facade name is what earns a bump."""
    assert scope_api.SCOPE_API_VERSION == 3
    scope_api.require_scope_api_version(3)
