from __future__ import annotations

from collections import UserDict
from types import MappingProxyType

import pytest

from salient_core.bus import _common
from salient_core.policy import decision, redaction


def test_sdk_invocation_has_builtin_canonical_identity() -> None:
    identity = decision.sdk_identity("Read", "researcher")
    invocation = decision.ToolInvocation.normalize(identity, {"file_path": "/tmp/a"})

    assert invocation.transport == decision.InvocationTransport.SDK
    assert invocation.wire_name == "Read"
    assert invocation.qualified_name == "builtin.Read"
    assert invocation.agent_id == "researcher"


def test_external_mcp_alias_canonicalizes_to_real_server() -> None:
    identity = decision.mcp_identity(
        "mcp__a__scan",
        "researcher",
        server_aliases={"a": "alpha"},
    )

    assert identity.transport == decision.InvocationTransport.MCP
    assert identity.qualified_name == "alpha.scan"


def test_bus_mcp_owner_and_alias_do_not_change_canonical_identity() -> None:
    direct = decision.mcp_identity("mcp__bus__researcher__ask_agent", "researcher")
    aliased = decision.mcp_identity("mcp__bus__r__ask_agent", "researcher")

    assert direct.qualified_name == "bus.ask_agent"
    assert aliased.qualified_name == direct.qualified_name


def test_mcp_looking_text_remains_text_with_bus_identity() -> None:
    identity = decision.text_identity("mcp__evil__scan", "researcher")

    assert identity.transport == decision.InvocationTransport.TEXT
    assert identity.wire_name == "mcp__evil__scan"
    assert identity.qualified_name == "bus.scan"


def test_invocation_snapshots_are_independent_deep_immutable_copies() -> None:
    original = {
        "request": {"password": "raw-password", "items": [{"target": "one"}]},
        "plain": "visible",
    }
    invocation = decision.ToolInvocation.normalize(
        decision.sdk_identity("Read", "researcher"),
        original,
    )

    original["request"]["password"] = "mutated-password"
    original["request"]["items"][0]["target"] = "two"

    assert invocation.evaluation_input["request"]["password"] == "raw-password"
    assert invocation.evaluation_input["request"]["items"][0]["target"] == "one"
    assert invocation.audit_input["request"]["password"] == "<redacted-secret>"
    assert invocation.audit_input["request"]["items"][0]["target"] == "one"
    assert "raw-password" not in repr(invocation.audit_input)
    assert "mutated-password" not in repr(invocation.audit_input)


def test_nested_user_mapping_is_independently_frozen_and_redacted() -> None:
    nested = UserDict({"password": "sensitive-placeholder", "items": [{"target": "one"}]})
    invocation = decision.ToolInvocation.normalize(
        decision.sdk_identity("Read", "researcher"),
        {"nested": nested},
    )

    nested["password"] = "changed-placeholder"
    nested["items"][0]["target"] = "two"

    assert invocation.evaluation_input["nested"]["password"] == "sensitive-placeholder"
    assert invocation.evaluation_input["nested"]["items"][0]["target"] == "one"
    assert invocation.audit_input["nested"]["password"] == "<redacted-secret>"
    assert "sensitive-placeholder" not in repr(invocation.audit_input)


def test_nested_mapping_proxy_is_independently_frozen_and_redacted() -> None:
    backing = {"password": "sensitive-placeholder", "target": "one"}
    nested = MappingProxyType(backing)
    invocation = decision.ToolInvocation.normalize(
        decision.sdk_identity("Read", "researcher"),
        {"nested": nested},
    )

    backing["password"] = "changed-placeholder"
    backing["target"] = "two"

    assert invocation.evaluation_input["nested"]["password"] == "sensitive-placeholder"
    assert invocation.evaluation_input["nested"]["target"] == "one"
    assert invocation.audit_input["nested"]["password"] == "<redacted-secret>"
    assert "sensitive-placeholder" not in repr(invocation.audit_input)


def test_nested_api_key_variants_are_redacted_from_args_and_audit_targets() -> None:
    invocation = decision.ToolInvocation.normalize(
        decision.sdk_identity("Read", "researcher"),
        {
            "args": {"api_key": "sensitive-placeholder"},
            "targets": [{"apikey": "second-sensitive-placeholder"}],
        },
    )

    assert invocation.evaluation_input["args"]["api_key"] == "sensitive-placeholder"
    assert invocation.audit_input["args"]["api_key"] == "<redacted-secret>"
    assert invocation.audit_input["targets"][0]["apikey"] == "<redacted-secret>"
    assert "sensitive-placeholder" not in repr(invocation.audit_input)


def test_direct_constructor_cannot_accept_caller_owned_snapshots() -> None:
    identity = decision.sdk_identity("Read", "researcher")

    with pytest.raises(TypeError):
        decision.ToolInvocation(identity, {"target": "raw"}, {"target": "raw"})


def test_neutral_decision_separates_policy_and_dispatch_allowance() -> None:
    result = decision.PolicyDecision(
        policy_allowed=False,
        dispatch_allowed=True,
        mode=decision.PolicyMode.SHADOW,
        policy_class="deny_unclassified",
        reason="classification missing",
        targets=("/tmp/a",),
        audit_event="builtin_policy_shadow",
        counter_delta=0,
    )

    assert result.policy_allowed is False
    assert result.dispatch_allowed is True
    assert result.allow is False


def test_malformed_nested_input_is_rejected_at_normalization_boundary() -> None:
    malformed = {"nested": {"mutable", "set"}}

    with pytest.raises(TypeError):
        decision.ToolInvocation.normalize(
            decision.sdk_identity("Read", "researcher"),
            malformed,
        )


def test_bus_redaction_compatibility_reexport_retains_nested_behavior() -> None:
    content = {"nested": [{"password": "raw-password"}], "plain": "visible"}

    redacted = _common._redact_secret_fields(content, tool="Read")

    assert redacted == {
        "nested": [{"password": "<redacted-secret>"}],
        "plain": "visible",
    }


def test_credential_marker_registration_is_idempotent() -> None:
    marker = "todo_one_idempotent_marker"

    redaction.register_cred_tool_markers([marker])
    count_after_first = len(redaction._EXTRA_CRED_TOOL_MARKERS)
    redaction.register_cred_tool_markers([marker])

    assert len(redaction._EXTRA_CRED_TOOL_MARKERS) == count_after_first


def test_cred_redaction_marker_uses_canonical_identity_not_model_wire_name() -> None:
    # A model can emit a text call whose wire name embeds "cred_record" while it
    # dispatches as a different bus tool. The credential redactor must key off the
    # canonical qualified identity so the model cannot suppress its own audit by
    # naming the tool, nor trigger cred redaction of an unrelated tool's fields.
    forged = decision.text_identity("cred_record__context_write", "researcher")
    assert forged.qualified_name == "bus.context_write"

    invocation = decision.ToolInvocation.normalize(
        forged,
        {"value": "not-a-credential-placeholder"},
    )

    # The generic cred-only field ("value") is NOT redacted, because the resolved
    # identity is bus.context_write, not a credential tool.
    assert invocation.audit_input["value"] == "not-a-credential-placeholder"

    # A genuine credential call keys on its canonical bus.cred_record identity.
    genuine = decision.ToolInvocation.normalize(
        decision.text_identity("mcp__forged__cred_record", "researcher"),
        {"value": "s3cr3t-placeholder"},
    )
    assert genuine.qualified_name == "bus.cred_record"
    assert genuine.audit_input["value"] == "<redacted-secret>"


def test_extended_auth_header_field_names_are_redacted() -> None:
    invocation = decision.ToolInvocation.normalize(
        decision.sdk_identity("Read", "researcher"),
        {
            "headers": {
                "authorization": "Bearer placeholder",
                "x-api-key": "placeholder-key",
                "safe": "keep",
            },
            "access_token": "token-placeholder",
        },
    )

    audit = invocation.audit_input
    assert audit["headers"]["authorization"] == "<redacted-secret>"
    assert audit["headers"]["x-api-key"] == "<redacted-secret>"
    assert audit["headers"]["safe"] == "keep"
    assert audit["access_token"] == "<redacted-secret>"
    # Raw evaluation snapshot still sees the real values for matching.
    assert invocation.evaluation_input["access_token"] == "token-placeholder"


def test_non_string_top_level_key_is_normalized_across_both_snapshots() -> None:
    invocation = decision.ToolInvocation.normalize(
        decision.sdk_identity("Read", "researcher"),
        {7: "seven"},
    )

    assert invocation.evaluation_input["7"] == "seven"
    assert invocation.audit_input["7"] == "seven"
