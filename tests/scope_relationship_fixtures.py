from __future__ import annotations

import functools
from typing import Any

import anyio

from salient_core.policy import scope
from salient_core.policy.decision import (
    InvocationIdentity,
    InvocationTransport,
    ToolInvocation,
)
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.scope_evaluation import evaluate_scope


def dataset(spec: scope.ExtractorSpec) -> PolicyDataset:
    return PolicyDataset(
        tool_targets={"cloud.run": spec},
        prohibited_patterns={},
        loud_patterns={},
    )


def invocation(raw_input: dict[str, Any]) -> ToolInvocation:
    return ToolInvocation.normalize(
        InvocationIdentity(InvocationTransport.MCP, "run", "cloud.run", "scope-agent"),
        raw_input,
    )


def evaluate(
    spec: scope.ExtractorSpec,
    raw_input: dict[str, Any],
    store: scope.ScopeStore,
    *,
    mode: str = "enforce",
):
    return anyio.run(
        functools.partial(
            evaluate_scope,
            invocation(raw_input),
            store,
            dataset(spec),
            mode=mode,
        )
    )


def external_spec(
    *,
    local_only: bool = False,
    none: bool = False,
    credential_binding_required: bool = False,
) -> scope.ExtractorSpec:
    return scope.ExtractorSpec(
        fields={"request": "compound_cloud"},
        local_only=local_only,
        none=none,
        external_modes=scope.ExternalModeContract(
            selector_field="mode",
            variants=(
                scope.ExternalModeVariant(
                    selector="inventory",
                    required_target_kinds=frozenset({"host", "saas", "cloud"}),
                    required_relationships=(
                        scope.PrincipalResourceRequirement(
                            provider_kind="host",
                            principal_kind="saas",
                            resource_kind="cloud",
                        ),
                    ),
                    cross_tenant_grants=(
                        scope.CrossTenantGrant(
                            provider="aws",
                            principal=scope.TargetIdentity(
                                kind="saas",
                                value="saas:aws/username/profile-a",
                            ),
                            principal_tenant="tenant-a",
                            resource=scope.TargetIdentity(
                                kind="cloud",
                                value="cloud:aws/aws/s3/us-east-1/222222222222/bucket-b",
                            ),
                            resource_tenant="tenant-b",
                        ),
                    ),
                    provider_bindings=(
                        scope.ProviderTargetBinding(
                            provider="aws",
                            target=scope.TargetIdentity(kind="host", value="aws.example"),
                        ),
                    ),
                    credential_binding_required=credential_binding_required,
                ),
            ),
        ),
    )


def compound_extractor(ctx: scope.ExtractorCtx) -> scope.ExtractionResult:
    data = ctx.args[ctx.field]
    provider = scope.Target("host", data["provider"], "request.provider")
    principal = scope.Target("saas", data["principal"], "request.principal")
    resource = scope.Target("cloud", data["resource"], "request.resource")
    targets = tuple(
        target
        for target in (provider, principal, resource)
        if target.kind not in set(data.get("omit", []))
    )
    return scope.ExtractionResult(
        targets=targets,
        relationships=(
            scope.PrincipalResourceRelationship(
                provider=provider,
                principal=principal,
                principal_provider=data["principal_provider"],
                principal_tenant=data["principal_tenant"],
                resource=resource,
                resource_provider=data["resource_provider"],
                resource_tenant=data["resource_tenant"],
                credential_binding_id=data.get("credential_binding_id"),
                credential_configuration_id=data.get("credential_configuration_id"),
                principal_identity=data.get("principal_identity"),
            ),
        ),
    )


def request(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "mode": "inventory",
        "request": {
            "provider": "aws.example",
            "principal": "saas:aws/username/profile-a",
            "principal_provider": "aws",
            "principal_tenant": "tenant-a",
            "resource": "cloud:aws/aws/s3/us-east-1/111111111111/bucket-a",
            "resource_provider": "aws",
            "resource_tenant": "tenant-a",
        },
    }
    value.update(changes)
    return value
