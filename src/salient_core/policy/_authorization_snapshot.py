from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

RuleKind = Literal[
    "network", "host_glob", "host_exact", "wifi_bssid", "wifi_ssid", "repo", "cloud", "saas"
]
Direction = Literal["in", "out"]
Origin = Literal["engagement", "adhoc"]

DEFAULT_INTERNAL_TLDS: tuple[str, ...] = (
    ".local",
    ".internal",
    ".lan",
    ".corp",
    ".intranet",
    ".home.arpa",
)

SNAPSHOT_IDENTITIES = MappingProxyType(
    {
        "api": "scope-snapshot-v1",
        "canonicalization": "scope-canonical-v1",
        "extractor": "scope-extractor-v1",
        "policy": "scope-policy-v1",
        "schema": "scope-schema-v2",
    }
)
_BINDING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


@dataclass(frozen=True)
class ScopeRule:
    pattern: str
    kind: RuleKind
    direction: Direction
    origin: Origin
    added_by: str
    added_at: float
    expires_at: float | None = None
    one_shot: bool = False
    consumed_at: float | None = None
    reason: str = ""

    def is_active(self, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        if self.expires_at is not None and current >= self.expires_at:
            return False
        if self.one_shot and self.consumed_at is not None:
            return False
        return True


@dataclass(frozen=True)
class ResearchPolicy:
    mode: str = "public"
    in_rules: tuple[ScopeRule, ...] = ()
    internal_tlds: tuple[str, ...] = DEFAULT_INTERNAL_TLDS


@dataclass(frozen=True, slots=True)
class SnapshotDraft:
    rules: tuple[ScopeRule, ...]
    research: ResearchPolicy
    session_strict: bool
    resource_context_json: str
    credential_bindings: tuple[CredentialBindingMetadata, ...]
    # scope.local_targets: when True, local-NIC/loopback addresses go through
    # normal rule evaluation instead of being filtered out as operator-side.
    # Required positional (no default) so every draft site must confront it —
    # a defaulted field would silently reset the flag on checkpoint restore.
    local_targets: bool


@dataclass(frozen=True, slots=True)
class CredentialBindingMetadata:
    binding_id: str
    provider: str
    principal: str
    tenant: str
    configuration_id: str


@dataclass(frozen=True, slots=True)
class AuthorizationSnapshot:
    generation: int
    snapshot_id: str
    predecessor_id: str | None
    rules: tuple[ScopeRule, ...]
    rule_ids: tuple[str, ...]
    research: ResearchPolicy
    session_strict: bool
    resource_context_json: str
    credential_bindings: tuple[CredentialBindingMetadata, ...]
    local_targets: bool


class ScopeSnapshotError(RuntimeError):
    pass


class ScopeSnapshotStaleError(ScopeSnapshotError):
    pass


class ScopeSnapshotCompatibilityError(ScopeSnapshotError):
    pass


def stable_rule_id(rule: ScopeRule) -> str:
    canonical = json.dumps(dataclasses.asdict(rule), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse_credential_bindings(raw: Mapping[str, object]) -> tuple[CredentialBindingMetadata, ...]:
    bindings = []
    required = {"provider", "principal", "tenant", "configuration_id"}
    for binding_id, value in raw.items():
        if type(binding_id) is not str or _BINDING_ID.fullmatch(binding_id) is None:
            raise ScopeSnapshotCompatibilityError("credential binding identity is malformed")
        if not isinstance(value, Mapping) or set(value) != required:
            raise ScopeSnapshotCompatibilityError("credential binding metadata is malformed")
        fields = tuple(
            value[key] for key in ("provider", "principal", "tenant", "configuration_id")
        )
        if any(
            type(field) is not str
            or not field
            or field != field.strip()
            or len(field) > 512
            or any(unicodedata.category(character).startswith("C") for character in field)
            for field in fields
        ):
            raise ScopeSnapshotCompatibilityError("credential binding metadata is malformed")
        bindings.append(CredentialBindingMetadata(binding_id, *fields))
    return tuple(sorted(bindings, key=lambda binding: binding.binding_id))


def snapshot_payload(snapshot: AuthorizationSnapshot) -> str:
    rule_rows = []
    for rule, rule_id in zip(snapshot.rules, snapshot.rule_ids, strict=True):
        row = dataclasses.asdict(rule)
        row["rule_id"] = rule_id
        rule_rows.append(row)
    body = {
        "credential_bindings": [
            dataclasses.asdict(binding) for binding in snapshot.credential_bindings
        ],
        "generation": snapshot.generation,
        "identities": dict(SNAPSHOT_IDENTITIES),
        "predecessor_id": snapshot.predecessor_id,
        "research": {
            "in_rules": [dataclasses.asdict(rule) for rule in snapshot.research.in_rules],
            "internal_tlds": list(snapshot.research.internal_tlds),
            "mode": snapshot.research.mode,
        },
        "resource_context": json.loads(snapshot.resource_context_json),
        "rules": rule_rows,
        "session_strict": snapshot.session_strict,
    }
    # Backward-compatible persistence: the flag is opt-in (default off), so
    # it is serialized ONLY when set — snapshots persisted before the flag
    # existed carry no key and must still digest-match on parse.
    if snapshot.local_targets:
        body["local_targets"] = True
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def build_snapshot(
    generation: int,
    predecessor_id: str | None,
    draft: SnapshotDraft,
) -> AuthorizationSnapshot:
    provisional = AuthorizationSnapshot(
        generation=generation,
        snapshot_id="",
        predecessor_id=predecessor_id,
        rules=draft.rules,
        rule_ids=tuple(stable_rule_id(rule) for rule in draft.rules),
        research=draft.research,
        session_strict=draft.session_strict,
        resource_context_json=draft.resource_context_json,
        credential_bindings=draft.credential_bindings,
        local_targets=draft.local_targets,
    )
    return dataclasses.replace(
        provisional,
        snapshot_id=hashlib.sha256(snapshot_payload(provisional).encode()).hexdigest(),
    )


def parse_snapshot(payload_json: str, stored_id: str) -> AuthorizationSnapshot:
    try:
        payload = json.loads(payload_json)
        if hashlib.sha256(payload_json.encode()).hexdigest() != stored_id:
            raise ScopeSnapshotCompatibilityError("stored snapshot digest does not match content")
        if payload["identities"] != dict(SNAPSHOT_IDENTITIES):
            raise ScopeSnapshotCompatibilityError("stored snapshot identities are incompatible")
        if type(payload["generation"]) is not int or payload["generation"] < 0:
            raise ScopeSnapshotCompatibilityError("stored snapshot generation is malformed")
        if type(payload["session_strict"]) is not bool:
            raise ScopeSnapshotCompatibilityError("stored session posture is malformed")
        # Opt-in flag, absent in snapshots persisted before it existed.
        local_targets = payload.get("local_targets", False)
        if type(local_targets) is not bool:
            raise ScopeSnapshotCompatibilityError("stored local-targets posture is malformed")
        rules_list = payload["rules"]
        if not isinstance(rules_list, list):
            raise ScopeSnapshotCompatibilityError("stored snapshot rules are malformed")
        rules = []
        for row in rules_list:
            if not isinstance(row, dict) or not isinstance(row.get("rule_id"), str):
                raise ScopeSnapshotCompatibilityError("stored snapshot rule is malformed")
            rule = ScopeRule(**{key: value for key, value in row.items() if key != "rule_id"})
            if row["rule_id"] != stable_rule_id(rule):
                raise ScopeSnapshotCompatibilityError("stored snapshot rule identity is malformed")
            rules.append(rule)
        research_body = payload["research"]
        if not isinstance(research_body, dict) or research_body.get("mode") not in {
            "public",
            "allowlist",
            "off",
        }:
            raise ScopeSnapshotCompatibilityError("stored research mode is malformed")
        research = ResearchPolicy(
            mode=research_body["mode"],
            in_rules=tuple(ScopeRule(**row) for row in research_body["in_rules"]),
            internal_tlds=tuple(research_body["internal_tlds"]),
        )
        bindings_list = payload["credential_bindings"]
        if not isinstance(bindings_list, list):
            raise ScopeSnapshotCompatibilityError("stored credential bindings are malformed")
        bindings_by_id = {
            row["binding_id"]: {key: value for key, value in row.items() if key != "binding_id"}
            for row in bindings_list
            if isinstance(row, dict) and isinstance(row.get("binding_id"), str)
        }
        if len(bindings_by_id) != len(bindings_list):
            raise ScopeSnapshotCompatibilityError("stored credential bindings are malformed")
        draft = SnapshotDraft(
            rules=tuple(rules),
            research=research,
            session_strict=payload["session_strict"],
            resource_context_json=json.dumps(
                payload["resource_context"], sort_keys=True, separators=(",", ":")
            ),
            credential_bindings=parse_credential_bindings(bindings_by_id),
            local_targets=local_targets,
        )
        snapshot = build_snapshot(payload["generation"], payload["predecessor_id"], draft)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ScopeSnapshotCompatibilityError("stored snapshot is malformed") from exc
    if snapshot.snapshot_id != stored_id:
        raise ScopeSnapshotCompatibilityError("stored snapshot semantics do not match content")
    return snapshot
