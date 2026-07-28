from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from salient_core.policy import scope
from salient_core.policy.scope_evaluation import ScopeEvaluationKind
from tests.scope_relationship_fixtures import compound_extractor, evaluate, external_spec, request
from tests.test_scope_credential_relationship_binding import _store

SENSITIVE_SENTINEL = "SENSITIVE_SENTINEL_4cf9"


def _secret_request(*, selector: bool = False) -> dict[str, object]:
    raw = request()
    raw["request"].update(
        {
            "password": SENSITIVE_SENTINEL,
            "provider_value": SENSITIVE_SENTINEL,
            "secret_selector": selector,
            "credential_configuration_id": "config-7",
        }
    )
    return raw


def _secret_extractor(ctx: scope.ExtractorCtx) -> scope.ExtractionResult:
    data = ctx.args[ctx.field]
    provider = scope.Target("host", data.get("provider_value", data["provider"]), "totally.safe")
    principal = scope.Target("saas", data["principal"], "request.principal")
    resource = scope.Target("cloud", data["resource"], "request.resource")
    return scope.ExtractionResult(
        targets=(provider, principal, resource),
        relationships=(
            scope.PrincipalResourceRelationship(
                provider=provider,
                principal=principal,
                principal_provider=data["principal_provider"],
                principal_tenant=data["principal_tenant"],
                resource=resource,
                resource_provider=data["resource_provider"],
                resource_tenant=data["resource_tenant"],
                credential_binding_id=data["password"] if data["secret_selector"] else "sel",
                credential_configuration_id=data["credential_configuration_id"],
                principal_identity=base64.urlsafe_b64encode(data["password"].encode()).decode(),
            ),
        ),
    )


def test_ordinary_relationship_metadata_remains_reconstructable(tmp_path: Path) -> None:
    # Given: ordinary nonsecret binding metadata and a real persistent store.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = _store(tmp_path)
    raw = request()
    raw["request"].update(
        {
            "credential_binding_id": "sel",
            "credential_configuration_id": "config-7",
            "principal_identity": "trusted-role",
        }
    )

    try:
        # When: the exact relationship is enforced.
        result = evaluate(external_spec(credential_binding_required=True), raw, store)

        # Then: its opaque identity remains unchanged in result and durable audit.
        row = store._conn.execute("SELECT relationships_json FROM scope_decisions").fetchone()
        durable = json.loads(row[0])[0]
        assert result.relationships[0].credential_binding_id == "sel"
        assert durable["credential_binding_id"] == "sel"
        assert durable["principal_identity"] == "trusted-role"
    finally:
        store.close()
        scope.unregister_all_extractors()


def test_secret_derived_relationship_scalar_is_redacted_everywhere(tmp_path: Path) -> None:
    # Given: a recognized secret emitted as an observed relationship principal.
    scope.register_extractor("compound_cloud", _secret_extractor)
    store = _store(tmp_path)

    try:
        # When: the snapshot binding denies the secret-derived principal.
        result = evaluate(
            external_spec(credential_binding_required=True),
            _secret_request(),
            store,
        )

        # Then: returned and durable audit facts contain only the redacted representation.
        row = store._conn.execute("SELECT * FROM scope_decisions").fetchone()
        assert result.allowed is False
        assert result.targets[0].value == "<redacted-secret>"
        assert result.relationships[0].principal_identity == "<redacted-secret>"
        assert SENSITIVE_SENTINEL not in json.dumps(list(row))
        assert "<redacted-secret>" in row[11]
    finally:
        store.close()
        scope.unregister_all_extractors()


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("credential_binding_id", True),
        ("credential_binding_id", {"sel": "x"}),
        ("credential_binding_id", "sel\u200d"),
        ("credential_configuration_id", False),
        ("credential_configuration_id", "config\u200d"),
        ("principal_identity", ["trusted-role"]),
        ("principal_identity", "trusted\u200drole"),
        ("principal_provider", True),
        ("principal_tenant", {"tenant": "a"}),
        ("resource_provider", ["aws"]),
        ("resource_tenant", "tenant\u0000a"),
    ],
)
def test_invalid_relationship_scalar_typed_denies_before_durable_fact(
    tmp_path: Path,
    field: str,
    invalid: object,
) -> None:
    # Given: a relationship extractor emits a non-string or control-bearing scalar.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = _store(tmp_path)
    raw = request()
    raw["request"].update(
        {
            "credential_binding_id": "sel",
            "credential_configuration_id": "config-7",
            "principal_identity": "trusted-role",
            field: invalid,
        }
    )

    try:
        # When: the invalid fact crosses the ExtractionResult boundary.
        result = evaluate(external_spec(credential_binding_required=True), raw, store)

        # Then: it is an extraction denial and no relationship fact is persisted.
        row = store._conn.execute("SELECT relationships_json FROM scope_decisions").fetchone()
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.EXTRACTION_DENIED
        assert result.relationships == ()
        assert json.loads(row[0]) == []
    finally:
        store.close()
        scope.unregister_all_extractors()


def test_secret_derived_binding_selector_typed_denies_and_redacts(tmp_path: Path) -> None:
    # Given: a recognized secret is emitted as the credential selector.
    scope.register_extractor("compound_cloud", _secret_extractor)
    store = _store(tmp_path)

    try:
        # When: the selector reaches the relationship boundary.
        result = evaluate(
            external_spec(credential_binding_required=True),
            _secret_request(selector=True),
            store,
        )

        # Then: it typed-denies and no raw selector reaches returned or durable evidence.
        row = store._conn.execute("SELECT * FROM scope_decisions").fetchone()
        assert result.allowed is False
        assert result.kind is ScopeEvaluationKind.EXTRACTION_DENIED
        assert SENSITIVE_SENTINEL not in json.dumps(result, default=str)
        assert SENSITIVE_SENTINEL not in json.dumps(list(row))
    finally:
        store.close()
        scope.unregister_all_extractors()


def test_encoded_and_generic_secret_field_names_are_normalized() -> None:
    # Given: encoded, control-bearing, case-varied, and generic secret keys.
    raw = {
        "request": {
            "api%5Fkey": SENSITIVE_SENTINEL,
            "api_key%00": SENSITIVE_SENTINEL,
            "PaSsWoRd": SENSITIVE_SENTINEL,
        },
        "token": SENSITIVE_SENTINEL,
    }

    # When: the transport-normalized invocation builds its audit input.
    from tests.scope_relationship_fixtures import invocation

    audit = invocation(raw).audit_input

    # Then: every canonical secret-key variant is structurally redacted.
    assert SENSITIVE_SENTINEL not in str(audit)


@pytest.mark.parametrize(("kind", "value"), [(True, "safe.example"), ("host", {"x": "y"})])
def test_invalid_registered_target_shape_typed_denies(
    tmp_path: Path,
    kind: object,
    value: object,
) -> None:
    # Given: a registered extractor emits a runtime-invalid Target shape.
    def malformed(ctx: scope.ExtractorCtx) -> list[scope.Target]:
        return [scope.Target(kind=kind, value=value, source_field="laundered.safe")]

    scope.register_extractor("compound_cloud", malformed)
    store = _store(tmp_path)
    try:
        # When: the target crosses the kernel-owned extraction boundary.
        result = evaluate(external_spec(credential_binding_required=True), request(), store)

        # Then: it typed-denies without returning or persisting the malformed target.
        row = store._conn.execute("SELECT targets_json FROM scope_decisions").fetchone()
        assert result.kind is ScopeEvaluationKind.EXTRACTION_DENIED
        assert result.targets == ()
        assert json.loads(row[0]) == []
    finally:
        store.close()
        scope.unregister_all_extractors()
