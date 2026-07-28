from __future__ import annotations

import json
from pathlib import Path

import pytest

from salient_core.policy import scope
from salient_core.policy.scope_evaluation import ScopeEvaluationKind
from tests.scope_relationship_fixtures import evaluate, external_spec, request
from tests.test_scope_credential_relationship_binding import _store


@pytest.mark.parametrize(
    ("member", "kind", "value", "source_field"),
    [
        ("provider", "host", {"oops": "x"}, "fake"),
        ("principal", "saas", {"oops": "x"}, "fake"),
        ("resource", "cloud", {"oops": "x"}, "fake"),
        ("provider", True, "provider.example", "fake"),
        ("principal", "saas", ["principal"], "fake"),
        ("resource", "cloud", "resource\u0000id", "fake"),
        ("provider", "host", " provider.example", "fake"),
        ("principal", "saas", "principal", False),
    ],
)
def test_malformed_relationship_only_target_typed_denies(
    tmp_path: Path,
    member: str,
    kind: object,
    value: object,
    source_field: object,
) -> None:
    # Given: a registered extractor embeds an invalid, unlisted Target in a relationship.
    def malformed(_ctx: scope.ExtractorCtx) -> scope.ExtractionResult:
        provider = scope.Target("host", "provider.example", "request.provider")
        principal = scope.Target("saas", "principal", "request.principal")
        resource = scope.Target("cloud", "resource", "request.resource")
        invalid = scope.Target(kind=kind, value=value, source_field=source_field)
        relationship = scope.PrincipalResourceRelationship(
            provider=invalid if member == "provider" else provider,
            principal=invalid if member == "principal" else principal,
            principal_provider="aws",
            principal_tenant="tenant-a",
            resource=invalid if member == "resource" else resource,
            resource_provider="aws",
            resource_tenant="tenant-a",
            credential_binding_id="sel",
            credential_configuration_id="config-7",
            principal_identity="trusted-role",
        )
        return scope.ExtractionResult(
            targets=(provider, principal, resource),
            relationships=(relationship,),
        )

    scope.register_extractor("compound_cloud", malformed)
    store = _store(tmp_path)
    try:
        # When: the complete target graph crosses the extraction boundary.
        result = evaluate(external_spec(credential_binding_required=True), request(), store)

        # Then: the malformed graph is denied without returned or durable facts.
        row = store._conn.execute(
            "SELECT targets_json, relationships_json FROM scope_decisions"
        ).fetchone()
        assert result.kind is ScopeEvaluationKind.EXTRACTION_DENIED
        assert result.targets == ()
        assert result.relationships == ()
        assert json.loads(row[0]) == []
        assert json.loads(row[1]) == []
    finally:
        store.close()
        scope.unregister_all_extractors()
