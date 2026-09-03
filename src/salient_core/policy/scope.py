"""Deterministic scope enforcement for tool invocations.

Every tool call is checked against an engagement allowlist (loaded from
`engagement.yaml`) plus operator-added adhoc rules, in pure Python,
BEFORE the tool subprocess is spawned. The LLM cannot bypass this — the
check happens inside the MCP handler wrapper, between the SDK routing
a ToolUseBlock and the tool's original body running.

Default is DENY. An engagement with no scope set refuses every
target-bearing tool call until the operator runs `prefs set scope.in_targets …`
or `salientctl scope add …`.

See docs/SCOPE.md for the full design — invariant, threat model, the
two scope sources, the four extractor kinds, the deny UX, and the
audit-log format.

Public surface (everything else is internal):

    Target, ScopeRule, Decision, CheckResult — data classes
    ExtractorError — raised by extractors when args can't be parsed
    ScopeStore — the one stateful object; owned by Daemon
    gate(sdk_tool, wire_name, agent_name, store) → SdkMcpTool
    TOOL_TARGETS — central extractor-spec table, classified per wire name
    parse_rule(pattern) → (kind, normalized_pattern)
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import re
import socket
import sqlite3
import subprocess
import time
import unicodedata
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, get_args

from . import _scope_schema
from . import resource_identity as _resource_identity
from ._authorization_snapshot import (
    DEFAULT_INTERNAL_TLDS,
    AuthorizationSnapshot,
    Direction,
    Origin,
    ResearchPolicy,
    RuleKind,
    ScopeRule,
    ScopeSnapshotCompatibilityError,
    ScopeSnapshotError,
    ScopeSnapshotStaleError,
    SnapshotDraft,
    build_snapshot,
    parse_credential_bindings,
    parse_snapshot,
    snapshot_payload,
    stable_rule_id,
)
from ._scope_schema import ScopeRuleSchemaError
from .decision import InvocationIdentity, InvocationTransport, ToolInvocation
from .scope_evaluation import evaluate_scope
from .scope_placeholders import unresolved_operator_infra_placeholder

ResourceIdentityError = _resource_identity.ResourceIdentityError
SCHEMA = _scope_schema.SCHEMA

if TYPE_CHECKING:
    from .registry import PolicyDataset

# ─── data model ─────────────────────────────────────────────────────────────

TargetKind = Literal[
    "ip", "network", "host", "url", "wifi_bssid", "wifi_ssid", "repo", "cloud", "saas"
]
Verdict = Literal["allow", "deny"]


@dataclass(frozen=True)
class Target:
    """One thing a tool wants to talk to.

    `value` is canonical: IPs/networks are `str(ipaddress.ip_*)`,
    hosts are lowercased + IDNA-normalized, URLs are reduced to host.
    """

    kind: TargetKind
    value: str
    source_field: str  # which arg field this came from, for error messages


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    """Stable target identity without extractor-local source metadata."""

    kind: TargetKind
    value: str


@dataclass(frozen=True, slots=True)
class ProviderTargetBinding:
    """Policy-authorized target identity for one canonical provider."""

    provider: str
    target: TargetIdentity


@dataclass(frozen=True, slots=True)
class CrossTenantGrant:
    """One exact provider, principal, tenant, and resource authorization."""

    provider: str
    principal: TargetIdentity
    principal_tenant: str
    resource: TargetIdentity
    resource_tenant: str


@dataclass(frozen=True, slots=True)
class PrincipalResourceRequirement:
    """Target kinds that must be joined by a principal-resource fact."""

    provider_kind: TargetKind
    principal_kind: TargetKind
    resource_kind: TargetKind


@dataclass(frozen=True, slots=True)
class PrincipalResourceRelationship:
    """Extractor-observed provider, principal, tenant, and resource relationship."""

    provider: Target
    principal: Target
    principal_provider: str
    principal_tenant: str
    resource: Target
    resource_provider: str
    resource_tenant: str
    credential_binding_id: str | None = None
    credential_configuration_id: str | None = None
    principal_identity: str | None = None
    source_field: str = ""


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Compound extraction output consumed by the single scope evaluator."""

    targets: tuple[Target, ...] = ()
    relationships: tuple[PrincipalResourceRelationship, ...] = ()

    def __post_init__(self) -> None:
        binding_id_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
        for target in self.targets:
            _validate_extracted_target(target)
        for relationship in self.relationships:
            _validate_extracted_target(relationship.provider)
            _validate_extracted_target(relationship.principal)
            _validate_extracted_target(relationship.resource)
            binding_id = relationship.credential_binding_id
            if binding_id is not None and (
                type(binding_id) is not str or binding_id_pattern.fullmatch(binding_id) is None
            ):
                raise ExtractorError("credential binding selector is malformed")
            for name, value in (
                ("principal provider", relationship.principal_provider),
                ("principal tenant", relationship.principal_tenant),
                ("resource provider", relationship.resource_provider),
                ("resource tenant", relationship.resource_tenant),
                ("credential configuration", relationship.credential_configuration_id),
                ("principal identity", relationship.principal_identity),
            ):
                if value is not None and (
                    type(value) is not str
                    or not value
                    or value != value.strip()
                    or len(value) > 512
                    or any(unicodedata.category(character).startswith("C") for character in value)
                ):
                    raise ExtractorError(f"relationship {name} is malformed")


def _validate_extracted_target(target: object) -> None:
    target_kinds = frozenset(get_args(TargetKind))
    if type(target) is not Target:
        raise ExtractorError("registered extractor target is malformed")
    if type(target.value) is str and not target.value.strip():
        raise ExtractorError(f"empty target value emitted for kind {target.kind!r}")
    if (
        type(target.kind) is not str
        or target.kind not in target_kinds
        or type(target.value) is not str
        or target.value != target.value.strip()
        or len(target.value) > 4096
        or any(unicodedata.category(character).startswith("C") for character in target.value)
        or type(target.source_field) is not str
        or not target.source_field
        or target.source_field != target.source_field.strip()
        or len(target.source_field) > 512
        or any(unicodedata.category(character).startswith("C") for character in target.source_field)
    ):
        raise ExtractorError("registered extractor target is malformed")


def _anchor_registered_extraction(
    extracted: list[Target] | ExtractionResult,
    field: str,
) -> ExtractionResult:
    match extracted:
        case ExtractionResult(targets=targets, relationships=relationships):
            anchored_targets = tuple(replace(target, source_field=field) for target in targets)
            target_map = dict(zip(targets, anchored_targets, strict=True))
            anchored_relationships = tuple(
                replace(
                    relationship,
                    provider=target_map.get(
                        relationship.provider,
                        replace(relationship.provider, source_field=field),
                    ),
                    principal=target_map.get(
                        relationship.principal,
                        replace(relationship.principal, source_field=field),
                    ),
                    resource=target_map.get(
                        relationship.resource,
                        replace(relationship.resource, source_field=field),
                    ),
                    source_field=field,
                )
                for relationship in relationships
            )
            return ExtractionResult(anchored_targets, anchored_relationships)
        case list() as targets:
            for target in targets:
                _validate_extracted_target(target)
            return ExtractionResult(
                tuple(replace(target, source_field=field) for target in targets)
            )


@dataclass(frozen=True, slots=True)
class ExternalModeContractError(ValueError):
    """An external mode contract is ambiguous or cannot require authorization."""

    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class ExternalModeVariant:
    """Exact output and relationship floor for one external execution mode."""

    selector: str
    required_target_kinds: frozenset[TargetKind]
    required_relationships: tuple[PrincipalResourceRequirement, ...] = ()
    cross_tenant_grants: tuple[CrossTenantGrant, ...] = ()
    provider_bindings: tuple[ProviderTargetBinding, ...] = ()
    credential_binding_required: bool = False

    def __post_init__(self) -> None:
        if not self.selector:
            raise ExternalModeContractError("external mode selector cannot be empty")
        if not self.required_target_kinds:
            raise ExternalModeContractError(
                f"external mode {self.selector!r} requires at least one target kind"
            )
        providers = tuple(binding.provider for binding in self.provider_bindings)
        if len(frozenset(providers)) != len(providers):
            raise ExternalModeContractError(
                f"external mode {self.selector!r} provider bindings must be unique"
            )


@dataclass(frozen=True, slots=True)
class ExternalModeContract:
    """Closed selector-to-contract mapping for externally reaching modes."""

    selector_field: str
    variants: tuple[ExternalModeVariant, ...]

    def __post_init__(self) -> None:
        if not self.selector_field:
            raise ExternalModeContractError("external mode selector field cannot be empty")
        selectors = tuple(variant.selector for variant in self.variants)
        if not selectors:
            raise ExternalModeContractError("external mode contract requires a variant")
        if len(frozenset(selectors)) != len(selectors):
            raise ExternalModeContractError("external mode selectors must be unique")

    def resolve(self, selector: str) -> ExternalModeVariant | None:
        return next((variant for variant in self.variants if variant.selector == selector), None)


@dataclass(frozen=True)
class Decision:
    target: Target
    verdict: Verdict
    matched_rule: ScopeRule | None
    reason: str


@dataclass(frozen=True)
class CheckResult:
    allowed: bool
    decisions: list[Decision]
    summary: str
    snapshot_id: str = ""
    snapshot_generation: int = 0
    relationship_denied: bool = False

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                stable_rule_id(decision.matched_rule)
                for decision in self.decisions
                if decision.matched_rule is not None
            )
        )


@dataclass(frozen=True, slots=True)
class ScopeCheckpoint:
    """Opaque exact-state checkpoint created by :meth:`ScopeStore.checkpoint`."""

    _store_identity: int
    _snapshot: AuthorizationSnapshot


class ExtractorError(ValueError):
    """An extractor refused the args (unparseable, obfuscated, required field
    missing). The gate denies with `str(exc)` as the reason."""


# ─── pattern parsing ───────────────────────────────────────────────────────


def parse_rule(
    pattern: str,
    *,
    force_kind: str | None = None,
) -> tuple[RuleKind, str]:
    """Classify and canonicalize a rule pattern.

    Raises ValueError if the pattern is not a recognized shape.

    Returns (kind, normalized_pattern):
        "10.0.0.0/24"      → ("network",   "10.0.0.0/24")
        "10.0.0.5"         → ("network",   "10.0.0.5/32")    # single IP → /32 network
        "2001:db8::/64"    → ("network",   "2001:db8::/64")
        "*.example.internal" → ("host_glob", "*.example.internal")
        "example.internal"   → ("host_exact","example.internal")

    `force_kind="wifi"` switches to wifi parsing:
        "70:a7:41:e1:05:96" → ("wifi_bssid", "70:A7:41:E1:05:96")
        "70-a7-41-e1-05-96" → ("wifi_bssid", "70:A7:41:E1:05:96")
        "MyHomeWiFi"        → ("wifi_ssid",  "MyHomeWiFi")     # case preserved
    """
    if force_kind == "wifi":
        return _parse_wifi_rule(pattern)
    if force_kind not in (None, "wifi"):
        raise ValueError(f"unsupported force_kind {force_kind!r}")
    authored = (pattern or "").strip()
    s = authored.lower().rstrip(".")
    if not s:
        raise ValueError("empty pattern")

    # Network / IP
    if _looks_ip_or_network(s):
        try:
            if "/" in s:
                net = ipaddress.ip_network(s, strict=False)
            else:
                addr = ipaddress.ip_address(s)
                # Single IP → /32 or /128 network for uniform membership check.
                net = ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False)
            return "network", str(net)
        except ValueError as e:
            raise ValueError(f"bad network pattern {pattern!r}: {e}") from e

    # A valid IPv6 address may begin with an alphabetic hextet (for example,
    # ``dead:beef::1``). Resource-family dispatch must therefore run only
    # after the legacy IP/network grammar has had the first opportunity.
    if _resource_identity.looks_tagged(authored):
        return _resource_identity.parse_resource_identity(authored)

    # Host glob (must contain *)
    if "*" in s:
        # Require the wildcard to be a leading "*." and the rest be a valid
        # hostname suffix. We don't accept arbitrary fnmatch patterns —
        # that produces too much surface area for ambiguous globs.
        if not s.startswith("*."):
            raise ValueError(f"only leading '*.suffix' wildcards are supported, got {pattern!r}")
        suffix = s[2:]
        if not suffix or not _looks_hostname(suffix):
            raise ValueError(f"bad host-glob pattern {pattern!r}")
        return "host_glob", s

    # Bare hostname
    if _looks_hostname(s):
        return "host_exact", s

    raise ValueError(
        f"unrecognized scope pattern {pattern!r} — expected IP, CIDR, hostname, or '*.suffix' glob"
    )


# ─── wifi (BSSID / SSID) parsing ───────────────────────────────────────────
#
# 802.11 BSSID is a MAC address; canonical form is six uppercase hex pairs
# joined by ':' (IEEE 802 std). Operators paste in colon-, dash-, or dot-
# separated forms — we normalize to the canonical form for storage.
#
# SSIDs are 0-32 byte strings, case-sensitive per 802.11. We require 1-32
# chars and preserve case exactly.

_RX_BSSID = re.compile(r"^[0-9a-f]{2}([:\-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}$", re.IGNORECASE)


def _normalize_bssid(s: str) -> str:
    """Canonicalize a BSSID/MAC to 'XX:XX:XX:XX:XX:XX' (uppercase, colons).
    Raises ValueError on bad shape."""
    s = (s or "").strip()
    if not _RX_BSSID.match(s):
        raise ValueError(f"bad BSSID {s!r} — expected 6 hex pairs separated by ':' or '-'")
    return s.upper().replace("-", ":")


def _looks_bssid(s: str) -> bool:
    return bool(_RX_BSSID.match((s or "").strip()))


def _parse_wifi_rule(pattern: str) -> tuple[RuleKind, str]:
    """Parse a `--wifi` scope pattern.

    BSSID shape (`XX:XX:XX:XX:XX:XX` or dashes) → wifi_bssid (canon uppercase
    colons). Anything else → wifi_ssid (1-32 chars, case preserved).
    """
    s = (pattern or "").strip()
    if not s:
        raise ValueError("empty pattern")
    if _looks_bssid(s):
        return "wifi_bssid", _normalize_bssid(s)
    if len(s.encode("utf-8")) > 32:
        raise ValueError(f"SSID {s!r} exceeds 32 bytes — 802.11 SSIDs are 0-32 bytes")
    return "wifi_ssid", s


_RX_IPV4_SHAPE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?$")
_RX_IPV6_SHAPE = re.compile(r"^[0-9a-f:]+(?:/\d{1,3})?$", re.IGNORECASE)
_RX_HOSTNAME = re.compile(
    r"^(?=.{1,253}\.?$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)

# Common file-extension TLDs that pattern-match the hostname regex but are
# obviously local filenames (pcap captures, hash dumps, config files,
# binaries, scripts, etc.). When a binary_argv / raw_argv token has one of
# these as its final label, classify it as "not a host" so the scope gate
# doesn't refuse on what's actually a file path. New extensions: lowercase,
# no dot, append below.
_FILE_EXT_TLDS = frozenset(
    {
        # Packet captures
        "pcap",
        "pcapng",
        "cap",
        # Generic data formats
        "txt",
        "log",
        "json",
        "csv",
        "tsv",
        "xml",
        "yaml",
        "yml",
        "toml",
        "ini",
        "md",
        "rst",
        "html",
        "htm",
        "pdf",
        # Binaries / objects
        "bin",
        "exe",
        "dll",
        "so",
        "elf",
        "obj",
        "o",
        "a",
        "lib",
        "ko",
        # Scripts / source
        "py",
        "sh",
        "rb",
        "pl",
        "ps1",
        "bat",
        "cmd",
        "js",
        "ts",
        "go",
        "rs",
        "c",
        "h",
        "cpp",
        "cc",
        "java",
        "class",
        "jar",
        "wasm",
        # Archives
        "zip",
        "tar",
        "gz",
        "bz2",
        "xz",
        "7z",
        "rar",
        # Tool-specific outputs
        "hash",
        "hashes",
        "nessus",
        "nmap",
        "gnmap",
        "kdbx",
        "ovpn",
        "creds",
        "loot",
        "wordlist",
        # Images / media (occasionally come through)
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "ico",
    }
)


def _is_file_extension_tld(s: str) -> bool:
    """Return True if `s` has a final label matching a known file-extension
    TLD — i.e. it looks like `something.pcap`, not like a real hostname."""
    tail = s.rsplit(".", 1)
    if len(tail) != 2:
        return False
    return tail[1].lower() in _FILE_EXT_TLDS


def _looks_ip_or_network(s: str) -> bool:
    return bool(_RX_IPV4_SHAPE.match(s)) or bool(_RX_IPV6_SHAPE.match(s) and ":" in s)


def _looks_hostname(s: str) -> bool:
    if _is_file_extension_tld(s):
        return False
    return bool(_RX_HOSTNAME.match(s))


# ─── target normalization ──────────────────────────────────────────────────

_RX_IP_RANGE = re.compile(r"^(?P<base>\d{1,3}\.\d{1,3}\.\d{1,3}\.)(?P<lo>\d{1,3})-(?P<hi>\d{1,3})$")


def _looks_ip_range(token: str) -> bool:
    return bool(_RX_IP_RANGE.match((token or "").strip()))


def _expand_range_token(token: str, source_field: str) -> list[Target]:
    """Expand hyphenated IP ranges like `10.0.0.5-10` into individual
    Targets. Returns [] when the token isn't a range (caller should fall
    through to single-token classify). Used by cidr_list (which gets
    these from a range-style `--target 10.0.0.5-10` arg) and ip_or_host
    (single host/IP target).

    Refuses ranges with > 256 hosts to avoid pathological inputs blowing
    up the scope check. Range bounds are validated."""
    t = (token or "").strip()
    m = _RX_IP_RANGE.match(t)
    if not m:
        return [_classify_token(t, source_field=source_field)] if t else []
    base = m.group("base")
    lo = int(m.group("lo"))
    hi = int(m.group("hi"))
    if lo > hi or lo > 255 or hi > 255:
        raise ExtractorError(f"field {source_field!r} bad IP range {t!r}: bounds {lo}-{hi}")
    if hi - lo > 255:
        raise ExtractorError(
            f"field {source_field!r} range {t!r} exceeds /24 width — break it into smaller chunks"
        )
    out: list[Target] = []
    for i in range(lo, hi + 1):
        ip = f"{base}{i}"
        try:
            out.append(_classify_token(ip, source_field=source_field))
        except ExtractorError:
            pass
    if not out:
        raise ExtractorError(f"field {source_field!r} range {t!r} expanded to zero valid IPs")
    return out


def _classify_token(token: str, source_field: str) -> Target:
    """Decide whether `token` is an IP, network, host, or URL.

    Raises ExtractorError if the token is none of those.
    """
    t = (token or "").strip().rstrip(".")
    if not t:
        raise ExtractorError(f"empty value in field {source_field!r}")

    # SSH-style "[user@]host" — strip the user prefix when the token is
    # not a URL (URLs handle user info via urlparse below). Only one '@'
    # is treated as the SSH form to avoid mis-parsing pathological input.
    # Benefits ssh.* tools and other host-targeting shell commands.
    if "@" in t and t.count("@") == 1 and not t.lower().startswith(("http://", "https://")):
        user_part, host_part = t.split("@", 1)
        if user_part and host_part:
            t = host_part

    # URL? (http/https)
    if t.lower().startswith(("http://", "https://")):
        try:
            u = urllib.parse.urlparse(t)
        except ValueError as e:
            raise ExtractorError(f"unparseable URL in {source_field!r}: {e}") from e
        if not u.hostname:
            raise ExtractorError(f"URL in {source_field!r} has no host: {t!r}")
        # Reduce URL to its hostname for scope-check purposes.
        host = u.hostname.lower()
        return _classify_token(host, source_field)  # recurse to resolve ip vs host

    # IP / network shape?
    if _looks_ip_or_network(t):
        try:
            if "/" in t:
                net = ipaddress.ip_network(t, strict=False)
                return Target(kind="network", value=str(net), source_field=source_field)
            addr = ipaddress.ip_address(t)
            return Target(kind="ip", value=str(addr), source_field=source_field)
        except ValueError as e:
            raise ExtractorError(f"bad IP/network in {source_field!r}: {e}") from e

    # Hostname?
    if _looks_hostname(t):
        try:
            normalized = t.encode("idna").decode("ascii").lower()
        except UnicodeError:
            normalized = t.lower()
        return Target(kind="host", value=normalized, source_field=source_field)

    raise ExtractorError(
        f"unrecognized target {t!r} in field {source_field!r} — "
        f"expected IP, CIDR, hostname, or http(s) URL"
    )


def _classify_endpoint(token: str, source_field: str) -> Target:
    """Classify a connection endpoint that may carry a scheme and/or port,
    then reduce it to the host for the scope check.

    Handles the shapes the `protocol` tools pass: ``host:port`` (grpc/tls),
    ``ws://host:port/path`` / ``wss://…`` (websockets), ``[::1]:443``
    (bracketed IPv6), and a bare host/IP (mqtt broker, raw banner-grab).
    Any scheme is accepted — we only care about the host for scope. Raises
    ExtractorError if no host can be recovered."""
    t = (token or "").strip()
    if not t:
        raise ExtractorError(f"empty value in field {source_field!r}")
    # scheme://… (ws/wss/grpc/http/…) — let urlparse pull the hostname.
    if "://" in t:
        try:
            u = urllib.parse.urlparse(t)
        except ValueError as e:
            raise ExtractorError(f"unparseable endpoint in {source_field!r}: {e}") from e
        if not u.hostname:
            raise ExtractorError(f"endpoint in {source_field!r} has no host: {t!r}")
        return _classify_token(u.hostname, source_field)
    # Bracketed IPv6, optionally with a port: [::1] or [::1]:443.
    if t.startswith("["):
        host = t[1:].split("]", 1)[0]
        return _classify_token(host, source_field)
    # host:port — strip a single trailing numeric port. A bare IPv6
    # (e.g. ::1) has more than one colon and is left intact for the
    # IP classifier below.
    if t.count(":") == 1:
        host, _, port = t.partition(":")
        if host and port.isdigit():
            return _classify_token(host, source_field)
    return _classify_token(t, source_field)


# ─── extractors ────────────────────────────────────────────────────────────

ExtractorKind = Literal[
    "ip_or_host",
    "ip_optional",
    "host",
    "host_optional",
    "url",
    "url_or_host",
    "endpoint",
    "cidr_list",
    "raw_argv",
    "binary_argv",
    "wifi_bssid",
    "wifi_ssid",
    "wifi_bssid_optional",
    "wifi_ssid_optional",
    "local_only",
    "none",
]
"""Generic extractor kinds the kernel handles inline. Domain-specific kinds are
supplied by a downstream skin via `register_extractor` and travel as plain
strings in `ExtractorSpec.fields`, so that field is typed `str`, not this
Literal — the Literal is documentation for the kernel's own kinds."""


@dataclass(frozen=True)
class ExtractorSpec:
    """Per-tool declaration of where targets live in the args dict.

    `fields` maps arg-name → extractor kind. `local_only` is a shortcut
    for tools that don't network at all (the gate logs an allow + skips
    the check). `none` is for bus tools and others that should never be
    scope-checked.

    `at_least_one`: when True, every listed field is treated as optional;
    extract whatever's present. After all fields are processed, if zero
    targets were extracted, raise. Use when several alternative target
    fields could satisfy the tool — e.g. tools that accept
    either `domain` (host) or `target` (IP).

    `refuse_unparseable`: when True (default), raw_argv commands refuse
    commands with shell substitution, hex-encoded IPs, etc.
    """

    fields: Mapping[str, str] = field(default_factory=dict)
    local_only: bool = False
    none: bool = False
    at_least_one: bool = False
    refuse_unparseable: bool = True
    session_scoped: bool = False
    """When True, this is a relayed command surface (a downstream skin's
    established-session/agent relay) that runs THROUGH that session/agent.
    It is scope-checked ONLY when the engagement opts in via
    `scope.session_strict` (`ScopeStore.session_strict()`); otherwise the gate
    bypasses it like `none` (legacy established-session trust). When strict is
    on, the `session_command` extractor sweeps the command string for embedded
    out-of-scope targets (relay defense). See SC-1 / docs/SCOPE.md."""
    research: bool = False
    """When True, this tool is discovery/recon (it gathers public data, it does
    not act on the host). The gate checks its targets against the broad RESEARCH
    lane (`ScopeStore.check_research`) instead of the strict engagement scope:
    engagement-in-scope ∪ public-internet, minus a hard floor that always
    denies private/internal ranges + `out_targets`. Lets research agents reach
    public sites without a per-site `scope add`, while never widening the
    engagement scope. See `research_config_from_profile` + docs/SCOPE.md."""
    research_active: bool = False
    """When True (implies research), this research tool RESOLVES AND TOUCHES
    the target (active DNS query / probe / crawl). The public floor therefore
    fails CLOSED on a resolution failure: a host the daemon can't resolve
    can't be verified public, and an active tool might still reach it via a
    split-horizon resolver. Passive DB-lookup research tools
    (research=True, research_active=False) fail OPEN — they only ever query
    public databases, never the target itself."""
    external_modes: ExternalModeContract | None = None
    """Required outputs and relationships for each externally reaching mode."""

    def __post_init__(self) -> None:
        # Freeze `fields` so registered policy cannot change through a retained
        # caller reference or a nested mutation of `dataset.tool_targets[…].fields`
        # after `set_active()`. `dict(...)` decouples from the caller's original
        # mapping (blocks mutating the source dict); MappingProxyType makes the
        # stored copy read-only (blocks direct mutation of the registered spec).
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def has_any_target_field(self) -> bool:
        return bool(self.fields)


# ─── extractor-kind registry (seam) ─────────────────────────────────────────
#
# The kernel implements a fixed set of GENERIC extractor kinds inline in
# `_extract_one` (ip_or_host, host, url, cidr_list, raw_argv, …). A downstream
# skin registers DOMAIN-SPECIFIC kinds (its own tool grammars) via
# `register_extractor`, consulted at call time. Generic kinds are reserved: a
# skin cannot shadow one. A kind that is neither inline nor registered raises
# the same ExtractorError as before (`unknown extractor kind: …`) — the public
# kernel's behavior for a kind no skin has installed.

SCOPE_API_VERSION = 3
"""Bumped when the extractor facade (ExtractorCtx + exported helpers) changes
in a way a skin must adapt to. A skin asserts compatibility at startup."""


@dataclass(frozen=True)
class ExtractorCtx:
    """What a registered (skin) extractor receives. `raw` is `args.get(field)`
    already fetched; the extractor reads any additional sibling fields it needs
    straight from `args` (e.g. a registered extractor may read a sibling field
    and args['options'])."""

    args: dict[str, Any]
    field: str
    optional: bool
    raw: Any


Extractor = Callable[["ExtractorCtx"], list[Target] | ExtractionResult]

# Kinds handled inline by `_extract_one` — reserved; a skin may not register
# these. Derived from the ExtractorKind Literal so it narrows automatically as
# domain-specific kinds are removed from the kernel.
_CORE_KINDS: frozenset[str] = frozenset(get_args(ExtractorKind))

_EXTRACTORS: dict[str, Extractor] = {}


def register_extractor(kind: str, fn: Extractor, *, override: bool = False) -> None:
    """Register a downstream extractor kind. Rejects reserved core kinds and
    silent duplicates — clobbering an extractor on a scope boundary is a
    regression, not a convenience. Pass override=True to intentionally replace."""
    if kind in _CORE_KINDS:
        raise ExtractorError(f"cannot register reserved core extractor kind: {kind!r}")
    if kind in _EXTRACTORS and not override:
        raise ExtractorError(f"extractor kind already registered: {kind!r}")
    _EXTRACTORS[kind] = fn


def unregister_all_extractors() -> None:
    """Clear the registry. Test-only — call between tests so a registration in
    one test can't leak into another."""
    _EXTRACTORS.clear()


# obfuscation indicators that refuse_unparseable=True should refuse on.
#
# `\xNN` / `0xHEXHEX…` patterns only refuse when long enough to plausibly
# encode an IP — a 4-byte IPv4 IP needs 8 hex digits (e.g. 0x0a000005 =
# 10.0.0.5). Shorter forms like `\x00`, `\xff` are common in legitimate
# byte-level work (shellcode generation, writing null terminators) and
# refusing them blocks legitimate binary output dumps or python `b'\x00'`
# string. The 4-byte threshold (8 hex digits) catches the obfuscation pattern
# without the false positives.
_RX_OBFUSCATION = re.compile(
    r"\$\(|`|<\(|>\(|"
    r"(?:\\x[0-9a-f]{2}){4,}|"  # 4+ consecutive \xNN bytes (IP-shaped)
    r"0x[0-9a-f]{8,}|"  # decimal-or-hex IP encoding (≥8 hex digits)
    r"\$\{[a-z_][a-z_0-9]*\}|\$[a-z_][a-z_0-9]*",
    re.IGNORECASE,
)
# In a raw_argv context, env-var refs are only OK if the var was set in
# the same invocation: `RHOST=10.0.0.5 nmap $RHOST`. Cheap detect: if every
# $VAR in the string has a matching VAR=... earlier in the string, allow.
_RX_ENVSET = re.compile(r"^([A-Z_][A-Z_0-9]*)=", re.IGNORECASE)
_RX_ENVREF = re.compile(r"\$\{?([A-Z_][A-Z_0-9]*)\}?", re.IGNORECASE)

# ── encoded-payload execution ────────────────────────────────────────────────
# A decode/transform wrapper that turns an opaque blob back into runnable
# text/bytes. On its own this is harmless (`base64 -d blob > out.bin`); it only
# hides a target when its output is fed to a dynamic-exec sink (below).
_RX_DECODE_WRAPPER = re.compile(
    r"\bb64decode\b|\bbase64\b\s*(?:\.\w+|-d\b|--decode\b)|"
    r"\bunhexlify\b|\bfromhex\b|"
    r"\bcodecs\.(?:decode|getdecoder)\b|"
    r"rot[_-]?13",
    re.IGNORECASE,
)
# A sink that runs decoded text/bytes as code. `exec`/`eval`/`os.system`/
# `subprocess`/`Popen` and shell `| sh` / `sh -c` are the common ones.
_RX_EXEC_SINK = re.compile(
    r"\bexec\s*\(|\beval\s*\(|\bexecfile\b|\bos\.system\b|\bsubprocess\b|"
    r"\bPopen\b|\bcheck_output\b|\bcheck_call\b|"
    r"\|\s*(?:ba)?sh\b|\b(?:ba)?sh\s+-c\b",
    re.IGNORECASE,
)
# An address reassembled from adjacent quoted fragments joined by `+`
# (`'1'+'0.'+'0.'+'0.'+'5'`) never presents a contiguous dotted-quad/IPv6 to the
# token sweep. Require a `.` or `:` inside at least one of the two joined
# fragments so plain word concatenation (`'a'+'b'`) is not refused.
_RX_SPLICED_ADDRESS = re.compile(
    r"""['"][0-9a-f]*[.:][0-9a-f.:]*['"]\s*\+\s*['"][0-9a-f.:]*['"]"""
    r"""|['"][0-9a-f.:]*['"]\s*\+\s*['"][0-9a-f]*[.:][0-9a-f.:]*['"]""",
    re.IGNORECASE,
)


def _encoded_exec_obfuscation(text: str) -> str | None:
    """Refuse a decode/transform wrapper whose output feeds a dynamic-exec sink
    (e.g. `python -c "exec(base64.b64decode('…'))"`, `base64 -d blob | sh`,
    `codecs.decode(x,'rot13')` into `exec`). The decoded payload never reaches
    the token sweep, so the real target is invisible to scope. Requiring BOTH a
    wrapper AND a sink keeps false positives bounded — a lone decoder that writes
    to a file, or a lone `subprocess` call with no encoding, is left alone."""
    if _RX_DECODE_WRAPPER.search(text) and _RX_EXEC_SINK.search(text):
        return "encoded payload fed to a dynamic-exec sink"
    return None


_RX_IPV4_TOKEN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")
_RX_URL_TOKEN = re.compile(r"https?://[^\s'\"`<>|;&]+", re.IGNORECASE)
# IPv6 CANDIDATE token: 2+ colon-separated hex groups (so a single-colon
# `host:port` or a `12:00` time never matches), optional `::` compression,
# optional `%zone` id, optional `/prefix`. This is deliberately loose — real
# validation is `ipaddress.ip_address` inside `_classify_token`, which drops
# any candidate that isn't a genuine IPv6 address (the sweep swallows the
# ExtractorError). Without this, an IPv6 target in a raw_argv command extracts
# nothing → the scope gate never checks it (only an IPv4 regex existed before).
# Boundaries exclude ALL word chars (not just hex) so a scope-resolution token
# like `sekurlsa::logonpasswords` or `Foo::Bar` — `word::word`, which looks like
# compressed IPv6 — is not matched mid-identifier. A real address is preceded by
# whitespace / `[` / `=` / `@` / quote, all non-word.
_RX_IPV6_TOKEN = re.compile(
    r"(?<![\w:%.])"
    r"(?:[0-9a-f]{0,4}:){2,}[0-9a-f]{0,4}"
    r"(?:%[0-9a-z_.-]+)?"
    r"(?:/\d{1,3})?"
    r"(?![\w:%.])",
    re.IGNORECASE,
)
# Boundary assertions:
#   (?<!\\)  — refuse matches preceded by a literal backslash, so `\n`, `\t`,
#              `\x`, `\u` escapes inside python/JSON string literals don't
#              eat the escape char as part of the hostname (live false
#              positive 2026-05-13: `\nsubprocess.call` was parsed as host
#              `nsubprocess.call`).
#   (?!\()   — refuse matches immediately followed by `(`, which always
#              signals a method/function call (`s.fileno()`, `os.path.join(`),
#              never a real hostname-in-command.
#   (?!\[)   — same idea for subscripts: `e.symbols['main']`, `cfg.hosts[0]`.
#              A DNS name is never indexed (live false positive 2026-07-29:
#              a pwntools script's `e.symbols['main']` extracted host
#              `e.symbols`). Generic on purpose — it retires the whole
#              `obj.attr[...]` class rather than one more attribute name.
_RX_HOST_TOKEN = re.compile(
    r"(?<!\\)\b(?![\d])[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b(?!\()(?!\[)",
    re.IGNORECASE,
)


# Labels that the host regex catches but that are almost never real DNS
# TLDs in command-line contexts — file extensions, Python attribute names,
# common method calls. Real DNS hostnames end in a registered TLD;
# `mine.sh`, `os.environ`, `t.connect`, `walk.py` all look hostname-shaped
# to the regex but are local scripts / Python code.
#
# This filter ONLY applies to the opportunistic regex sweep inside raw_argv
# /binary_argv. Explicit target fields (a tool's explicit target=/url=
# url=) are not affected — operators who scope `mine.sh` deliberately
# still get the explicit-field path.
_NOT_A_REAL_TLD: frozenset[str] = frozenset(
    {
        # ── file extensions ────────────────────────────────────────────────
        "py",
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "psm1",
        "psd1",
        "bat",
        "cmd",
        "txt",
        "md",
        "rst",
        "json",
        "yml",
        "yaml",
        "toml",
        "cfg",
        "ini",
        "log",
        "env",
        "lock",
        "bak",
        "swp",
        "swo",
        "swn",
        "tmp",
        "sock",
        "pid",
        "key",
        "crt",
        "pem",
        "csr",
        "cer",
        "p12",
        "pfx",
        "conf",
        "service",
        "target",
        "timer",
        "socket",
        "html",
        "htm",
        "css",
        "js",
        "jsx",
        "ts",
        "tsx",
        "mjs",
        "cjs",
        "c",
        "cpp",
        "cc",
        "cxx",
        "h",
        "hpp",
        "hxx",
        "java",
        "rb",
        "php",
        "go",
        "rs",
        "lua",
        "sql",
        "db",
        "sqlite",
        "sqlite3",
        "dbf",
        "jpg",
        "jpeg",
        "png",
        "gif",
        "svg",
        "ico",
        "bmp",
        "webp",
        "tiff",
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "odt",
        "ods",
        "odp",
        "zip",
        "tar",
        "gz",
        "bz2",
        "xz",
        "7z",
        "rar",
        "tgz",
        "tbz",
        "tlz",
        "lzma",
        "zst",
        "exe",
        "dll",
        "so",
        "dylib",
        "out",
        "bin",
        "deb",
        "rpm",
        "pkg",
        "dmg",
        "iso",
        "img",
        "vhd",
        "vmdk",
        "qcow2",
        "ova",
        "ovf",
        "mp3",
        "mp4",
        "wav",
        "avi",
        "mov",
        "mkv",
        "flac",
        "ogg",
        "m4a",
        "webm",
        "whl",
        "egg",
        "jar",
        "war",
        "ear",
        "class",
        # Mobile build artifacts — the `jar` family's siblings, and the
        # everyday operands of any tool that unpacks or repacks an app
        # bundle. None is a registered TLD.
        "apk",
        "ipa",
        "dex",
        "aab",
        "ko",
        "mod",
        "map",
        "sym",
        # ── Python / shell attribute + method names commonly dotted in code
        "write",
        "read",
        "close",
        "open",
        "connect",
        "send",
        "recv",
        "accept",
        "bind",
        "listen",
        "shutdown",
        "get",
        "set",
        "put",
        "post",
        "delete",
        "update",
        "insert",
        "remove",
        "clear",
        "append",
        "extend",
        "pop",
        "push",
        "shift",
        "unshift",
        "slice",
        "splice",
        "join",
        "split",
        "replace",
        "sub",
        "match",
        "search",
        "find",
        "filter",
        "reduce",
        "sorted",
        "reversed",
        "each",
        "keys",
        "values",
        "items",
        "environ",
        "stdout",
        "stderr",
        "stdin",
        "argv",
        "path",
        "getenv",
        "setenv",
        "transport",
        "filemode",
        "rstrip",
        "lstrip",
        "strip",
        "casefold",
        "format",
        "dumps",
        "loads",
        "dump",
        "load",
        "decode",
        "encode",
        "pack",
        "unpack",
        "sftpclient",
        "sftpattributes",
        "filename",
        "name",
        "title",
        "exists",
        "mkdir",
        "makedirs",
        "rmdir",
        "unlink",
        "walk",
        "listdir",
        "scandir",
        "isfile",
        "isdir",
        "abspath",
        "dirname",
        "basename",
        "splitext",
        "realpath",
        "relpath",
        "commonpath",
        "getuid",
        "geteuid",
        "getpid",
        "getppid",
        "getgid",
        "getegid",
        "cwd",
        "getcwd",
        "chdir",
        "system",
        "popen",
        "fork",
        "exec",
        "execv",
        "execve",
        "execvp",
        "fileno",
        "call",
        "check_call",
        "check_output",
        "run",
        "communicate",
        "meta",  # Splunk app metadata (default.meta, local.meta)
        "time",
        "sleep",
        "monotonic",
        "gmtime",
        "localtime",
        "strftime",
        "strptime",
        "lower",
        "upper",
        "capitalize",
        "center",
        "ljust",
        "rjust",
        # ── Python imaging / data-science attribute names ──────────────────
        # Hit live 2026-05-12: agent ran a PIL script, scope flagged
        # `pil.exiftags`, `img.size`, `img.mode` as out-of-scope hostnames.
        "size",
        "mode",
        "exif",
        "exiftags",
        "tags",
        "shape",
        "dtype",
        "ndim",
        "itemsize",
        "nbytes",
        "index",
        "columns",
        "axes",
        "info",
        "palette",
        "getexif",
        "getdata",
        "thumbnail",
        "crop",
        "rotate",
        "transpose",
        "histogram",
        # Common pandas / numpy method-suffixes
        "iloc",
        "loc",
        "iat",
        "at",
        "iterrows",
        "itertuples",
        "tolist",
        # Pillow / PIL module names that show up dotted
        "image",
        "imagedraw",
        "imagefont",
        "imageops",
    }
)


def _is_real_hostname_shape(token: str, extra: frozenset[str] = frozenset()) -> bool:
    """Return False for tokens that look hostname-shaped to the regex but
    are almost certainly Python attribute access (`os.environ`) or file
    extensions (`mine.sh`, `walk.py`). Used to filter the regex sweep
    inside raw_argv / binary_argv extraction.

    Heuristics:
      - Last label not in _NOT_A_REAL_TLD
      - Last label is 2+ alphabetic chars (no all-numeric/short TLDs)

    Filtering is conservative: explicit-field extraction (nmap target=,
    ffuf url=) bypasses this entirely. Operators who scope a `.sh` domain
    use that explicit path."""
    parts = token.lower().rstrip(".").split(".")
    if len(parts) < 2:
        return False
    last = parts[-1]
    if last in _NOT_A_REAL_TLD or last in extra:
        return False
    if len(last) < 2 or not last.isalpha():
        return False
    return True


def _is_path_segment(text: str, start: int) -> bool:
    """True if the hostname-shaped match at `start` is really a component of a
    filesystem path (`./app.apk`, `out/app.apk`, `/opt/tools/app.apk`).

    Live false positive (2026-07-29): `apktool d ./app.apk` extracted host
    `app.apk`, so a downstream tool that exists to unpack a local artifact was
    scope-refused on its own operand. Extending the file-extension denylist only
    ever chases the extension of the week; the structural signal is the separator.

    The rule is "preceded by `/` inside the same shell word" — with one carve-out
    that must not regress: `//host/share` (UNC / scheme-relative) names a GENUINELY
    REMOTE host and has to stay scope-checked. So a match sitting at the first
    component after a word's leading slash run is a host, not a path segment;
    anything deeper is a path segment.

    Direction of error is deliberate. Suppressing a real remote host would open a
    scope-blind hole; refusing a local file is only noise. So this suppresses ONLY
    on an explicit separator and never guesses from the token's own shape.
    """
    if start == 0 or text[start - 1] != "/":
        return False
    # Walk back to the start of the whitespace-delimited word.
    word_start = start
    while word_start > 0 and not text[word_start - 1].isspace():
        word_start -= 1
    # `//host/share` / `///a` — the first component after the leading slash run
    # is a remote host, not a path segment.
    lead = word_start
    while lead < len(text) and text[lead] == "/":
        lead += 1
    if lead > word_start + 1 and lead == start:
        return False
    return True


def _sweep_tokens(
    text: str,
    field: str,
    *,
    extra_not_tld: frozenset[str] = frozenset(),
) -> list[Target]:
    """Opportunistic URL/IP/host regex sweep over free-form command text.

    Used by raw_argv (and skin-registered command extractors): URLs first (their spans
    masked so an embedded host isn't double-reported), then IPv4 tokens, then
    hostname-shaped tokens that survive `_is_real_hostname_shape`. Tokens that
    fail classification are skipped (not every hostname-shaped token is a real
    target). `extra_not_tld` adds caller-specific labels to the host filter."""
    targets: list[Target] = []
    seen_spans: list[tuple[int, int]] = []
    for m in _RX_URL_TOKEN.finditer(text):
        try:
            targets.append(_classify_token(m.group(0), source_field=field))
            seen_spans.append((m.start(), m.end()))
        except ExtractorError:
            pass
    for m in _RX_IPV4_TOKEN.finditer(text):
        if _overlaps(m.start(), m.end(), seen_spans):
            continue
        tok = m.group(0)
        # A dotted-quad with a zero-padded octet (012.0.0.5) is octal IP
        # obfuscation — refuse loudly rather than silently drop it (it would
        # otherwise fail strict IP parse and be swallowed, leaving the command
        # target-less -> allowed).
        if _has_leading_zero_octet(tok):
            raise ExtractorError(
                f"octal-encoded IP {tok!r} (leading-zero octet; inet_aton "
                f"reads it as octal). Use canonical dotted notation."
            )
        try:
            targets.append(_classify_token(tok, source_field=field))
            seen_spans.append((m.start(), m.end()))
        except ExtractorError:
            pass
    for m in _RX_IPV6_TOKEN.finditer(text):
        if _overlaps(m.start(), m.end(), seen_spans):
            continue
        # Drop a `%zone` scope-id (`fe80::1%eth0`) before classifying: it's
        # irrelevant to the scope check and `_classify_token`'s shape gate
        # rejects the `%`, which would otherwise let a zoned address slip
        # through as an unrecognized (→ unchecked) token.
        cand = m.group(0).split("%", 1)[0]
        # A colon-only match (`::`, `:::`) is the unspecified/loopback shorthand
        # with no hextet — skip it rather than mint a spurious `::` target from a
        # stray double-colon in prose/code.
        if not any(c in "0123456789abcdefABCDEF" for c in cand):
            continue
        try:
            # `_classify_token` runs `ipaddress.ip_address`, so a candidate that
            # isn't a real IPv6 address (`12:00:00`, `a:b:c`) raises and is
            # skipped here — only genuine addresses become scope-checked targets.
            targets.append(_classify_token(cand, source_field=field))
            seen_spans.append((m.start(), m.end()))
        except ExtractorError:
            pass
    for m in _RX_HOST_TOKEN.finditer(text):
        if _overlaps(m.start(), m.end(), seen_spans):
            continue
        tok = m.group(0)
        if _is_path_segment(text, m.start()):
            continue
        if not _is_real_hostname_shape(tok, extra_not_tld):
            continue
        try:
            targets.append(_classify_token(tok, source_field=field))
            seen_spans.append((m.start(), m.end()))
        except ExtractorError:
            pass
    return targets


def _is_locally_bound(var_name: str, text_before: str) -> bool:
    """True if `$VAR` was bound earlier in the same command string.
    Covers the common bash idioms operators actually use:
        VAR=value …
        for VAR in …; do
        while read VAR
        read -r VAR
        select VAR in …
        getopts ... VAR
    Without these, my earlier "VAR=" check would refuse every legitimate
    bash one-liner that loops over files (`for f in *.txt; do … $f …`)."""
    n = re.escape(var_name)
    patterns = (
        rf"\b{n}\s*=",  # NAME=value
        rf"\bfor\s+{n}\s+in\b",  # for f in …
        rf"\bread\s+(?:-\w+\s+)*(?:[A-Za-z_][\w]*\s+)*{n}\b",  # read … f
        rf"\bselect\s+{n}\s+in\b",  # select f in …
        rf"\bgetopts\s+\S+\s+{n}\b",  # getopts spec f
    )
    return any(re.search(p, text_before) for p in patterns)


# A standalone run of 8-10 digits — the width of a 32-bit integer (2**24 =
# 16777216 is 8 digits, 2**32-1 = 4294967295 is 10). Bounded by non-word,
# non-dot so it never fires inside `0x0a000005`, a dotted-quad octet, or a
# longer identifier. Range is checked in code (regex can't compare magnitude).
_RX_BARE_INT = re.compile(r"(?<![\w.])(\d{8,10})(?![\w.])")


def _bare_int_ip_encoding(text: str) -> str | None:
    """Refuse a standalone integer that inet_aton would read as a full IPv4
    address (>= 2**24, so the high byte is non-zero — e.g. 167772165 = 10.0.0.5).

    Unlike `0x…` hex, a bare integer carries no explicit encoding signal, so we
    can't tell "encoded target" from "large count/timestamp" — and this only
    runs in the raw_argv obfuscation path, where a target-bearing command that
    names an integer-encoded host would otherwise slip the scope gate entirely
    (empty extraction -> allowed). Fail closed: the operator restates a real
    target in dotted notation. Values < 2**24 (ports, small counts) are left
    alone; dotted forms are handled by the IPv4 sweep."""
    for m in _RX_BARE_INT.finditer(text):
        val = int(m.group(1))
        if (1 << 24) <= val < (1 << 32):
            dotted = ipaddress.ip_address(val)
            return (
                f"integer-encoded IP {m.group(1)} (inet_aton reads it as "
                f"{dotted}). Use canonical dotted notation ({dotted}) so the "
                f"scope gate can check the target."
            )
    return None


def _has_leading_zero_octet(token: str) -> bool:
    """True if a dotted-quad-shaped token has a zero-padded octet (012.0.0.5).
    inet_aton reads a leading-zero octet as OCTAL (012 -> 10), so this is IP
    obfuscation; no legitimate address zero-pads its octets."""
    host = token.split("/", 1)[0]
    parts = host.split(".")
    if len(parts) != 4:
        return False
    return any(len(p) > 1 and p[0] == "0" for p in parts)


def _is_obfuscated(text: str) -> str | None:
    """Return a description if the text contains obfuscation we refuse, else None."""
    # Bare integer-encoded IPs carry no `0x`-style signal the regex can anchor
    # on, so check them independently of (and before) the pattern sweep.
    bare = _bare_int_ip_encoding(text)
    if bare:
        return bare
    enc = _encoded_exec_obfuscation(text)
    if enc:
        return enc
    if _RX_SPLICED_ADDRESS.search(text):
        return "address spliced across adjacent string literals"
    m = _RX_OBFUSCATION.search(text)
    if not m:
        return None
    matched = m.group(0)
    # Allow env-var refs that are locally set in the same invocation.
    if matched.startswith("$"):
        var = _RX_ENVREF.match(matched)
        if var:
            name = var.group(1)
            if _is_locally_bound(name, text[: m.start()]):
                # Locally bound — try the next obfuscation match (recurse cheap).
                rest = text[m.end() :]
                inner = _is_obfuscated(rest)
                return inner
            return (
                f"reference to non-local env var ${name} (each bash.run "
                f"starts a fresh `bash -c` — variables from a prior call "
                f"don't carry. Inline the value (`curl 10.0.0.5`) OR "
                f"set+use in ONE command (`{name}=10.0.0.5 && curl "
                f"${name}`))"
            )
    if matched == "`" or matched == "$(":
        return "command substitution"
    if matched.startswith(("<(", ">(")):
        return "process substitution"
    if matched.startswith(("\\x", "0x")):
        return f"hex-encoded byte/IP ({matched})"
    return f"refused construct: {matched!r}"


def _extract_one(
    args: dict[str, Any],
    field: str,
    kind: str,
    optional: bool = False,
) -> list[Target] | ExtractionResult:
    """Extract Target(s) for a single arg-field according to its kind.

    `optional`: when True, missing/empty values return [] instead of raising.
    Malformed values STILL raise even when optional — missing-field is OK,
    bad-data is not."""
    raw = args.get(field)

    placeholder = unresolved_operator_infra_placeholder(raw)
    if placeholder is not None:
        raise ExtractorError(
            f"field {field!r} contains unresolved operator-infrastructure "
            f"placeholder {placeholder!r}; substitute the engagement's real "
            "listener value before calling the tool"
        )

    if kind in ("ip_optional", "host_optional"):
        if raw in (None, "", []):
            return []
        kind = "ip_or_host" if kind == "ip_optional" else "host"  # fall through

    if kind in ("wifi_bssid_optional", "wifi_ssid_optional"):
        if raw in (None, "", []):
            return []
        kind = "wifi_bssid" if kind == "wifi_bssid_optional" else "wifi_ssid"

    # Domain-specific kinds a downstream skin registered — consulted BEFORE the
    # generic empty-guard so the extractor owns its own missing/empty semantics
    # (e.g. a relayed session command that names nothing is allowed, not
    # refused). A miss falls through to the kernel's inline generic handling.
    fn = _EXTRACTORS.get(kind)
    if fn is not None:
        extracted = fn(ExtractorCtx(args=args, field=field, optional=optional, raw=raw))
        return _anchor_registered_extraction(extracted, field)

    if raw in (None, "", []):
        if optional:
            return []
        raise ExtractorError(f"required field {field!r} is missing/empty")

    if kind == "wifi_bssid":
        token = str(raw).strip()
        try:
            value = _normalize_bssid(token)
        except ValueError as e:
            raise ExtractorError(f"field {field!r}: {e}") from e
        return [Target(kind="wifi_bssid", value=value, source_field=field)]

    if kind == "wifi_ssid":
        token = str(raw).strip()
        if not token:
            raise ExtractorError(f"field {field!r} expects an SSID, got empty")
        if len(token.encode("utf-8")) > 32:
            raise ExtractorError(f"field {field!r} SSID {token!r} exceeds 32 bytes")
        return [Target(kind="wifi_ssid", value=token, source_field=field)]

    if kind in ("ip_or_host", "host"):
        token = str(raw).strip()
        if kind == "ip_or_host" and _looks_ip_range(token):
            ts = _expand_range_token(token, source_field=field)
            if ts:
                return ts
        t = _classify_token(token, source_field=field)
        if kind == "host" and t.kind != "host":
            raise ExtractorError(f"field {field!r} expects a hostname, got {t.kind} {t.value!r}")
        if kind == "ip_or_host" and t.kind not in ("ip", "host", "network"):
            raise ExtractorError(f"field {field!r} expects ip/host, got {t.kind} {t.value!r}")
        return [t]

    if kind == "url":
        token = str(raw).strip()
        if not token.lower().startswith(("http://", "https://")):
            raise ExtractorError(f"field {field!r} expects a URL, got {token!r}")
        return [_classify_token(token, source_field=field)]

    if kind == "url_or_host":
        token = str(raw).strip()
        return [_classify_token(token, source_field=field)]

    if kind == "endpoint":
        return [_classify_endpoint(str(raw).strip(), source_field=field)]

    if kind == "cidr_list":
        if isinstance(raw, str):
            tokens = re.split(r"[,\s]+", raw.strip())
        elif isinstance(raw, list):
            tokens = [str(t).strip() for t in raw]
        else:
            raise ExtractorError(
                f"field {field!r} expects a list/string of CIDRs, got {type(raw).__name__}"
            )
        tokens = [t for t in tokens if t]
        if not tokens:
            raise ExtractorError(f"field {field!r} is empty after parsing")
        out: list[Target] = []
        for cidr in tokens:
            out.extend(_expand_range_token(cidr, source_field=field))
        return out

    if kind == "binary_argv":
        # The 11 agents whose `run` takes `binary: str` + `args: list` (not
        # `command: str`) — synthesize a single shell-style command line
        # from those two fields and parse it the same way as raw_argv.
        # `field` for binary_argv is conventionally "binary" — we ignore
        # raw and read both `binary` and `args` straight from the args dict.
        binary = str(args.get("binary") or "").strip()
        arglist = args.get("args")
        if arglist in (None, ""):
            arglist = []
        if not isinstance(arglist, list):
            raise ExtractorError(
                f"binary_argv: 'args' must be a list, got {type(arglist).__name__}"
            )
        joined = " ".join([binary] + [str(a) for a in arglist if str(a)]).strip()
        if not joined:
            if optional:
                return []
            raise ExtractorError("binary_argv: both 'binary' and 'args' are empty")
        # Re-enter the raw_argv branch with the synthesized string.
        return _extract_one(
            {"_argv_text": joined},
            "_argv_text",
            "raw_argv",
            optional=optional,
        )

    if kind == "raw_argv":
        # Tool may pass either a single string (`bash.run.command`) or a
        # list of strings. For lists, join with newlines so the regex sweep +
        # obfuscation detector see the whole multi-line script.
        if isinstance(raw, list):
            text = "\n".join(str(t) for t in raw if str(t).strip())
        else:
            text = str(raw)
        # Collapse Unicode compatibility forms (full-width / homoglyph digits and
        # letters) to their ASCII equivalents BEFORE detection + sweep, so an
        # address written with fancy digits (`１０.０.０.５`, `𝟣𝟢.𝟢.𝟢.𝟧`) reduces to
        # `10.0.0.5` and is caught by the same IPv4 / integer / host detectors as
        # its plain-ASCII spelling. Detection-only: the executed command is never
        # touched — we only inspect this normalized copy for scope-check purposes.
        text = unicodedata.normalize("NFKC", text)
        if not text.strip():
            if optional:
                return []
            raise ExtractorError(f"field {field!r} is empty after joining list elements")

        obf = _is_obfuscated(text)
        if obf:
            raise ExtractorError(
                f"refused: {field!r} contains {obf}. Set this command's "
                f"targets in flat form (e.g. `curl 10.0.0.5`, no $(), no "
                f"variable indirection, no hex-encoded IPs) and try again."
            )

        # URLs first (their spans masked internally so an embedded host isn't
        # double-reported), then IPs, then hostname-shaped tokens that survive
        # `_is_real_hostname_shape` (filters `os.environ`, `mine.sh`, etc.).
        targets: list[Target] = _sweep_tokens(text, field)
        if not targets:
            # No remote target named. The scope contract is "IF a command names
            # a target, that target must be in scope" — not "every command must
            # name one" (`ls /tmp`, `jobs -l`). A flat command that goes on the
            # wire would have produced an IP/host/URL token and been checked.
            #
            # BEST-EFFORT, NOT AUTHORITATIVE. This is a static regex sweep over
            # free-form shell/Python, so it is deliberately one layer of
            # defense-in-depth, not a sealed boundary. The obfuscation detector
            # above fails CLOSED on the evasions it recognizes (substitution,
            # encoded IPs, unbound $VARs, decode→exec, spliced literals), but a
            # sufficiently determined command can still name a target the sweep
            # can't see (runtime-computed strings, novel encodings, multi-call
            # /tmp indirection). Those are the message-bus + operator-approval
            # layers' job to catch — NOT a guarantee this function makes. Route
            # target-bearing work through the typed tool factories (nmap, ssh,
            # …) wherever possible rather than `*.run` shell escapes.
            return []
        return targets

    if kind == "none":
        return []

    raise ExtractorError(f"unknown extractor kind: {kind!r}")


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for s, e in spans:
        if start < e and s < end:
            return True
    return False


def extract_scope(spec: ExtractorSpec, args: dict[str, Any]) -> ExtractionResult:
    """Extract targets and typed relationship facts from one invocation."""
    if (spec.none or spec.local_only) and spec.external_modes is None:
        return ExtractionResult()
    targets: list[Target] = []
    relationships: list[PrincipalResourceRelationship] = []
    for field_name, kind in spec.fields.items():
        extracted = _extract_one(
            args,
            field_name,
            kind,
            optional=spec.at_least_one,
        )
        match extracted:
            case ExtractionResult(field_targets, field_relationships):
                targets.extend(field_targets)
                relationships.extend(field_relationships)
            case list() as field_targets:
                targets.extend(field_targets)
    for target in targets:
        placeholder = unresolved_operator_infra_placeholder(target.value)
        if placeholder is not None:
            raise ExtractorError(
                "extracted target contains unresolved operator-infrastructure "
                f"placeholder {placeholder!r} from field {target.source_field!r}; "
                "substitute the engagement's real target value before calling the tool"
            )
    if spec.at_least_one and spec.fields and not targets:
        raise ExtractorError(
            f"none of {list(spec.fields)} were set — at least one target "
            f"field is required; got args: {sorted(args)}"
        )
    return ExtractionResult(tuple(targets), tuple(relationships))


def extract_targets(spec: ExtractorSpec, args: dict[str, Any]) -> list[Target]:
    """Top-level: given a spec and the tool's call args, return the targets.

    Returns [] for `none` or `local_only` specs (the gate handles them
    separately). Raises ExtractorError if any field can't be parsed.

    When spec.at_least_one is True, individually missing/empty fields are
    skipped (instead of raising), but at the end we require at least one
    target overall — otherwise refuse with a clear error message.
    """
    return list(extract_scope(spec, args).targets)


def _validate_relationships(
    variant: ExternalModeVariant,
    extraction: ExtractionResult,
) -> str | None:
    required = variant.required_relationships
    if extraction.relationships and not required:
        return "unexpected relationship facts for mode without relationship requirements"
    target_set = frozenset(extraction.targets)
    grants = frozenset(variant.cross_tenant_grants)
    for relationship in extraction.relationships:
        if not {relationship.provider, relationship.principal, relationship.resource} <= target_set:
            return "relationship references a target absent from compound extraction"
        if not relationship.principal_provider or not relationship.resource_provider:
            return "relationship provider is missing"
        if relationship.principal_provider != relationship.resource_provider:
            return "principal and resource providers are incompatible"
        provider_binding = next(
            (
                binding
                for binding in variant.provider_bindings
                if binding.provider == relationship.principal_provider
            ),
            None,
        )
        if provider_binding is None:
            return "relationship provider has no authorized provider target binding"
        provider_identity = TargetIdentity(
            kind=relationship.provider.kind,
            value=relationship.provider.value,
        )
        if provider_identity != provider_binding.target:
            return "provider target is incompatible with relationship provider"
        if not relationship.principal_tenant or not relationship.resource_tenant:
            return "relationship tenant is missing"
        if relationship.principal_tenant != relationship.resource_tenant:
            grant = CrossTenantGrant(
                provider=relationship.principal_provider,
                principal=TargetIdentity(
                    kind=relationship.principal.kind,
                    value=relationship.principal.value,
                ),
                principal_tenant=relationship.principal_tenant,
                resource=TargetIdentity(
                    kind=relationship.resource.kind,
                    value=relationship.resource.value,
                ),
                resource_tenant=relationship.resource_tenant,
            )
            if grant not in grants:
                return (
                    "principal tenant does not own resource and has no explicit cross-tenant grant"
                )
    for requirement in required:
        facts = tuple(
            relationship
            for relationship in extraction.relationships
            if relationship.provider.kind == requirement.provider_kind
            and relationship.principal.kind == requirement.principal_kind
            and relationship.resource.kind == requirement.resource_kind
        )
        if not facts:
            return "missing required principal-resource relationship"
        for kind, label, covered in (
            (
                requirement.provider_kind,
                "provider",
                frozenset(relationship.provider for relationship in facts),
            ),
            (
                requirement.principal_kind,
                "principal",
                frozenset(relationship.principal for relationship in facts),
            ),
            (
                requirement.resource_kind,
                "resource",
                frozenset(relationship.resource for relationship in facts),
            ),
        ):
            expected = frozenset(target for target in extraction.targets if target.kind == kind)
            if expected - covered:
                return f"required {label} target is not covered by a relationship"
    return None


def _credential_binding_error(
    snapshot: AuthorizationSnapshot,
    relationship: PrincipalResourceRelationship,
    required: bool,
) -> str | None:
    if not required and relationship.credential_binding_id is None:
        return None
    binding_id = relationship.credential_binding_id
    if not binding_id:
        return "credential relationship is missing a binding selector"
    binding = next(
        (
            candidate
            for candidate in snapshot.credential_bindings
            if candidate.binding_id == binding_id
        ),
        None,
    )
    if binding is None:
        return "credential relationship references an unknown binding selector"
    if relationship.credential_configuration_id != binding.configuration_id:
        return "credential relationship references a stale configuration"
    if relationship.principal_provider != binding.provider:
        return "credential relationship provider does not match its binding"
    if relationship.principal_identity != binding.principal:
        return "credential relationship principal does not match its binding"
    if relationship.principal_tenant != binding.tenant:
        return "credential relationship tenant does not match its binding"
    return None


# ─── the store ─────────────────────────────────────────────────────────────

_RULE_KINDS = frozenset(get_args(RuleKind))
_DIRECTIONS = frozenset(get_args(Direction))
_ORIGINS = frozenset(get_args(Origin))


def _validate_persisted_scope_rules(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT kind,pattern,direction,origin FROM scope_rules").fetchall()
    for kind, pattern, direction, origin in rows:
        if kind not in _RULE_KINDS:
            raise ScopeRuleSchemaError(f"unknown persisted rule kind {kind!r}")
        if direction not in _DIRECTIONS:
            raise ScopeRuleSchemaError(f"unknown persisted rule direction {direction!r}")
        if origin not in _ORIGINS:
            raise ScopeRuleSchemaError(f"unknown persisted rule origin {origin!r}")
        try:
            if kind in {"repo", "cloud", "saas"}:
                resource_kind, canonical = _resource_identity.parse_resource_identity(pattern)
                if resource_kind != kind or canonical != pattern:
                    raise ScopeRuleSchemaError(
                        f"persisted {kind} identity is not canonical: {pattern!r}"
                    )
            elif kind in {"wifi_bssid", "wifi_ssid"}:
                wifi_kind, canonical = parse_rule(pattern, force_kind="wifi")
                if wifi_kind != kind or canonical != pattern:
                    raise ScopeRuleSchemaError(
                        f"persisted {kind} identity is not canonical: {pattern!r}"
                    )
            else:
                legacy_kind, canonical = parse_rule(pattern)
                if legacy_kind != kind or canonical != pattern:
                    raise ScopeRuleSchemaError(
                        f"persisted {kind} identity is not canonical: {pattern!r}"
                    )
        except ValueError as exc:
            raise ScopeRuleSchemaError(f"uninterpretable persisted rule identity: {exc}") from exc


# ─── operator-side IP detection ─────────────────────────────────────────────
#
# Any IPv4/IPv6 address assigned to a local NIC is, by definition, this box
# — i.e. the operator's own infrastructure. Such addresses commonly surface
# as LHOST / callback hosts inside payload source, curl command lines, or
# config files. They are never engagement targets, so the scope gate filters
# them out before evaluating rules. Cache the lookup so we don't fork `ip`
# on every tool call; refresh on a short TTL so a tun0 brought up mid-session
# starts getting exempted within a minute.

_LOCAL_ADDR_TTL = 60.0
_local_addr_cache: tuple[float, frozenset[Any]] | None = None


def _local_addresses() -> frozenset[Any]:
    """Frozen set of `ipaddress.ip_address` objects bound to local NICs.

    Reads `ip -j addr show`. If iproute2 is unavailable or returns
    unparseable output we fall back to {127.0.0.1, ::1} — degrading to a
    minimal exemption set rather than silently exempting everything.
    """
    global _local_addr_cache
    now = time.monotonic()
    if _local_addr_cache is not None:
        ts, cached = _local_addr_cache
        if now - ts < _LOCAL_ADDR_TTL:
            return cached
    addrs: set[Any] = set()
    try:
        proc = subprocess.run(
            ["ip", "-j", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        )
        data = json.loads(proc.stdout or "[]")
        for iface in data:
            for info in iface.get("addr_info") or []:
                local = info.get("local")
                if not local:
                    continue
                try:
                    addrs.add(ipaddress.ip_address(local))
                except ValueError:
                    continue
    except (subprocess.SubprocessError, FileNotFoundError, json.JSONDecodeError, OSError):
        addrs.update(
            {
                ipaddress.ip_address("127.0.0.1"),
                ipaddress.ip_address("::1"),
            }
        )
    if not addrs:
        addrs.update(
            {
                ipaddress.ip_address("127.0.0.1"),
                ipaddress.ip_address("::1"),
            }
        )
    frozen = frozenset(addrs)
    _local_addr_cache = (now, frozen)
    return frozen


def _is_operator_target(t: Target) -> bool:
    """True iff `t` is an IPv4/IPv6 address assigned to this host."""
    if t.kind != "ip":
        return False
    try:
        addr = ipaddress.ip_address(t.value)
    except ValueError:
        return False
    return addr in _local_addresses()


def _split_operator_targets(
    targets: list[Target],
) -> tuple[list[Target], list[Target]]:
    """Partition into (remote_targets, operator_side_targets)."""
    remote: list[Target] = []
    op: list[Target] = []
    for t in targets:
        (op if _is_operator_target(t) else remote).append(t)
    return remote, op


def _operator_filter_note(op_targets: list[Target]) -> str:
    """Comma-joined IP values for the filtered operator-side targets."""
    return ", ".join(sorted({t.value for t in op_targets}))


# ─── research lane (public-web access, separate from engagement scope) ────────
#
# The research lane lets discovery/recon tools (ExtractorSpec.research=True) reach
# the PUBLIC internet without a per-site `scope add`, while NEVER widening the
# engagement's scope. It is strictly ADDITIVE: a target is allowed iff it
# is already in engagement scope OR it passes the public floor below. The floor
# is the load-bearing safety invariant — research tools can never reach
# private/internal infra or `out_targets`.

_DEFAULT_INTERNAL_TLDS = DEFAULT_INTERNAL_TLDS
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")  # RFC 6598 shared space


def _norm_tld(x: str) -> str:
    s = str(x).strip().lower().rstrip(".")
    return s if s.startswith(".") else "." + s


def research_config_from_profile(profile: dict[str, Any] | None) -> ResearchPolicy:
    """Read the research lane policy from the engagement profile's
    `scope.research` block. Mirrors `safeguards.posture_from_profile`. Absent
    block → default `public` (on). `scope.research: off` (or `false`, or
    `{mode: off}`) → research tools fall back to strict engagement scope."""
    scope_block = (profile or {}).get("scope") or {}
    rb = scope_block.get("research")
    if rb is None:
        return ResearchPolicy()
    if rb is False or (isinstance(rb, str) and rb.strip().lower() == "off"):
        return ResearchPolicy(mode="off")
    if not isinstance(rb, dict):
        return ResearchPolicy()
    mode = str(rb.get("mode") or "public").strip().lower()
    if mode not in ("public", "allowlist", "off"):
        mode = "public"
    in_rules: list[ScopeRule] = []
    now = time.time()
    for pat in _as_list(_coalesce(rb, "in", "in_targets") or []):
        try:
            kind, norm = parse_rule(pat)
        except ValueError:
            continue
        in_rules.append(
            ScopeRule(
                pattern=norm,
                kind=kind,
                direction="in",
                origin="engagement",
                added_by="engagement.yaml:scope.research",
                added_at=now,
                reason="",
            )
        )
    # internal_tlds EXTENDS the built-in floor — it can only ADD names, never
    # remove a default. Otherwise an operator setting `internal_tlds: [.corp]`
    # would silently drop `.internal` (GCP/cloud metadata) + `.home.arpa`,
    # re-opening them to the lane. The floor only ever tightens.
    extra = tuple(_norm_tld(x) for x in _as_list(rb.get("internal_tlds") or []))
    tlds = _DEFAULT_INTERNAL_TLDS + tuple(t for t in extra if t not in _DEFAULT_INTERNAL_TLDS)
    return ResearchPolicy(mode=mode, in_rules=tuple(in_rules), internal_tlds=tlds)


# Resolver indirection so tests can monkeypatch `_resolve_host` for
# determinism (no real DNS). Results are cached with a short TTL. The cache is
# process-global (one ScopeStore per daemon); the public/private verdict is
# recomputed per call from the (re-resolved, TTL-refreshed) addrs, so a shared
# cache only saves lookups — it never carries a verdict across engagements.
_RESOLVE_TTL = 300.0
_RESOLVE_CACHE_MAX = 5000
_resolve_cache: dict[str, tuple[float, tuple[Any, ...] | None]] = {}

# Dedicated, BOUNDED pool for research-lane DNS so a hung resolver under a wide
# OSINT swarm caps its blast radius here instead of starving asyncio's shared
# default executor (which infra / report tools dispatch onto). getaddrinfo's C
# call can't be interrupted, so bounding the pool — not a per-call timeout — is
# the load-bearing mitigation.
_RESEARCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="scope-research",
)


def _resolve_host(host: str) -> tuple[Any, ...] | None:
    """Resolve `host` → tuple of ip_address objects, or None on failure.
    Monkeypatch this in tests. NOTE: getaddrinfo has no timeout argument; the
    OS resolver's own timeout bounds it, and the bounded `_RESEARCH_EXECUTOR`
    caps how many can hang at once."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError, UnicodeError):
        return None
    addrs: set[Any] = set()
    for info in infos:
        try:
            addrs.add(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue
    return tuple(addrs) or None


def _resolve_cached(host: str) -> tuple[Any, ...] | None:
    now = time.monotonic()
    hit = _resolve_cache.get(host)
    if hit is not None and now - hit[0] < _RESOLVE_TTL:
        return hit[1]
    res = _resolve_host(host)
    # Coarse cap so an OSINT burst over many distinct hosts can't grow the
    # cache without bound; drop the whole map when it's exceeded (simpler than
    # LRU, and entries are cheap to re-resolve).
    if len(_resolve_cache) >= _RESOLVE_CACHE_MAX:
        _resolve_cache.clear()
    _resolve_cache[host] = (now, res)
    return res


def _ip_is_global(ip: Any) -> bool:
    """True iff `ip` is a globally-routable public address (the research lane's
    allow condition). Denies private/loopback/link-local/reserved/multicast/
    unspecified, CGNAT shared space, and any address bound to a local NIC."""
    if ip in _local_addresses():
        return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False
    if ip.version == 4 and ip in _CGNAT_NET:
        return False
    return True


class ScopeStore:
    """Engagement-scoped allow/deny rule store plus the per-call gate.

    One instance per Daemon. Constructed at daemon boot from the engagement
    profile (engagement-origin rules) and from the on-disk scope.db
    (adhoc-origin rules, if any). Adhoc rules persist across daemon
    restarts within the same engagement; engagement rules are reloaded
    from engagement.yaml on every restart.

    All operations are synchronous; the daemon is single-event-loop so
    there's no thread-safety concern. SQLite uses WAL.
    """

    def __init__(
        self,
        db_path: Path | None,
        engagement_id: str,
    ) -> None:
        self.engagement_id = engagement_id
        self.db_path = db_path
        self._snapshot_fault: Literal["before_commit", "after_commit"] | None = None
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn: sqlite3.Connection | None = sqlite3.connect(
                str(db_path),
                isolation_level=None,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            _scope_schema.initialize_scope_schema(self._conn, _validate_persisted_scope_rules)
            self._snapshot = self._load_or_bootstrap_snapshot()
        else:
            self._conn = None
            self._snapshot = self._build_snapshot(
                0,
                None,
                SnapshotDraft((), ResearchPolicy(), False, "{}", (), False),
            )
        self._publish_snapshot(self._snapshot)

    @property
    def snapshot(self) -> AuthorizationSnapshot:
        return self._snapshot

    def checkpoint(self) -> ScopeCheckpoint:
        """Capture the exact live and durable authorization state for rollback."""
        if self._conn is not None:
            head = self._conn.execute(
                "SELECT generation FROM scope_head WHERE singleton=1"
            ).fetchone()
            if head is None or head[0] != self._snapshot.generation:
                raise ScopeSnapshotStaleError(
                    "authorization snapshot differs from the durable head"
                )
        return ScopeCheckpoint(id(self), self._snapshot)

    def restore_checkpoint(
        self,
        checkpoint: ScopeCheckpoint,
        *,
        expected_current: AuthorizationSnapshot,
    ) -> AuthorizationSnapshot:
        """Restore a checkpoint without creating a new authorization generation."""
        if checkpoint._store_identity != id(self):
            raise ScopeSnapshotCompatibilityError("scope checkpoint belongs to another store")
        snapshot = checkpoint._snapshot
        if self._conn is not None:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                head = self._conn.execute(
                    "SELECT s.generation,s.snapshot_id FROM scope_head h "
                    "JOIN scope_snapshots s ON s.generation=h.generation "
                    "WHERE h.singleton=1"
                ).fetchone()
                checkpoint_head = (snapshot.generation, snapshot.snapshot_id)
                expected_head = (
                    expected_current.generation,
                    expected_current.snapshot_id,
                )
                if head == checkpoint_head:
                    self._conn.execute("COMMIT")
                    self._publish_snapshot(snapshot)
                    return snapshot
                if head != expected_head:
                    raise ScopeSnapshotStaleError(
                        "authorization snapshot changed after owned publication"
                    )
                row = self._conn.execute(
                    "SELECT snapshot_id FROM scope_snapshots WHERE generation=?",
                    (snapshot.generation,),
                ).fetchone()
                if row is None or row[0] != snapshot.snapshot_id:
                    raise ScopeSnapshotCompatibilityError(
                        "scope checkpoint is absent from durable history"
                    )
                self._conn.execute(
                    "DELETE FROM scope_snapshots WHERE generation > ?",
                    (snapshot.generation,),
                )
                self._conn.execute("DELETE FROM scope_rules")
                for rule in snapshot.rules:
                    self._insert_rule_row(rule)
                changed = self._conn.execute(
                    "UPDATE scope_head SET generation=? WHERE singleton=1",
                    (snapshot.generation,),
                )
                if changed.rowcount != 1:
                    raise ScopeSnapshotCompatibilityError("scope head is missing")
                self._conn.execute("COMMIT")
            except BaseException:  # noqa: BLE001 — roll back, then re-raise unchanged
                # Interrupt-safe: a KeyboardInterrupt / CancelledError landing
                # between statements must not leave a dangling transaction that
                # wedges every later BEGIN IMMEDIATE. Roll back if still open.
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        elif self._snapshot is snapshot:
            return snapshot
        elif self._snapshot is not expected_current:
            raise ScopeSnapshotStaleError("authorization snapshot changed after owned publication")
        self._publish_snapshot(snapshot)
        return snapshot

    def _publish_snapshot(self, snapshot: AuthorizationSnapshot) -> None:
        self._snapshot = snapshot

    def _build_snapshot(
        self,
        generation: int,
        predecessor_id: str | None,
        draft: SnapshotDraft,
    ) -> AuthorizationSnapshot:
        return build_snapshot(generation, predecessor_id, draft)

    # ─── rule loading ────────────────────────────────────────────────────

    def _load_or_bootstrap_snapshot(self) -> AuthorizationSnapshot:
        assert self._conn is not None
        head = self._conn.execute("SELECT generation FROM scope_head WHERE singleton=1").fetchone()
        if head is None:
            rules = self._rules_from_current_table()
            snapshot = self._build_snapshot(
                0,
                None,
                SnapshotDraft(rules, ResearchPolicy(), False, "{}", (), False),
            )
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._insert_snapshot_row(snapshot)
                self._conn.execute("INSERT INTO scope_head(singleton,generation) VALUES(1,0)")
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
            return snapshot
        row = self._conn.execute(
            "SELECT snapshot_id,payload_json FROM scope_snapshots WHERE generation=?",
            (head[0],),
        ).fetchone()
        if row is None:
            raise ScopeSnapshotCompatibilityError("scope head references a missing snapshot")
        return self._parse_snapshot(row[1], row[0])

    def _rules_from_current_table(self) -> tuple[ScopeRule, ...]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT pattern,kind,direction,origin,added_by,added_at,"
            "expires_at,one_shot,consumed_at,reason FROM scope_rules"
        ).fetchall()
        return tuple(
            ScopeRule(
                pattern=row[0],
                kind=row[1],
                direction=row[2],
                origin=row[3],
                added_by=row[4],
                added_at=row[5],
                expires_at=row[6],
                one_shot=bool(row[7]),
                consumed_at=row[8],
                reason=row[9],
            )
            for row in rows
        )

    def _parse_snapshot(self, payload_json: str, stored_id: str) -> AuthorizationSnapshot:
        return parse_snapshot(payload_json, stored_id)

    def _insert_snapshot_row(self, snapshot: AuthorizationSnapshot) -> None:
        assert self._conn is not None
        payload = snapshot_payload(snapshot)
        self._conn.execute(
            "INSERT INTO scope_snapshots "
            "(generation,snapshot_id,predecessor_id,payload_json,committed_at) "
            "VALUES(?,?,?,?,?)",
            (
                snapshot.generation,
                snapshot.snapshot_id,
                snapshot.predecessor_id,
                payload,
                time.time(),
            ),
        )

    def _commit_snapshot(
        self,
        snapshot: AuthorizationSnapshot,
        expected_generation: int,
    ) -> AuthorizationSnapshot:
        if self._conn is None:
            if expected_generation != self._snapshot.generation:
                raise ScopeSnapshotStaleError("authorization snapshot generation is stale")
            self._publish_snapshot(snapshot)
            return snapshot
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            head = self._conn.execute(
                "SELECT generation FROM scope_head WHERE singleton=1"
            ).fetchone()
            if head is None or head[0] != expected_generation:
                raise ScopeSnapshotStaleError("authorization snapshot generation is stale")
            self._insert_snapshot_row(snapshot)
            self._conn.execute("DELETE FROM scope_rules")
            for rule in snapshot.rules:
                self._insert_rule_row(rule)
            changed = self._conn.execute(
                "UPDATE scope_head SET generation=? WHERE singleton=1 AND generation=?",
                (snapshot.generation, expected_generation),
            )
            if changed.rowcount != 1:
                raise ScopeSnapshotStaleError("authorization snapshot generation is stale")
            if self._snapshot_fault == "before_commit":
                raise RuntimeError("snapshot fault before_commit")
            self._conn.execute("COMMIT")
        except BaseException:  # noqa: BLE001 — roll back, then re-raise unchanged
            # Interrupt-safe (see restore_checkpoint): never leak an open
            # transaction, even on KeyboardInterrupt / CancelledError.
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        if self._snapshot_fault == "after_commit":
            raise RuntimeError("snapshot fault after_commit")
        self._publish_snapshot(snapshot)
        return snapshot

    def prepare_engagement_rules(
        self,
        profile: dict[str, Any],
        *,
        expected_generation: int | None = None,
    ) -> AuthorizationSnapshot:
        """Build an engagement-rule publication without committing it.

        The returned snapshot is an ownership token for compare-and-swap rollback.
        """
        scope_block = (profile or {}).get("scope") or {}
        # Research lane policy travels with the engagement scope block.
        research = research_config_from_profile(profile)
        # SC-1: opt-in per-call re-check of relayed session commands.
        session_strict = bool(
            _coalesce(
                scope_block,
                "session_strict",
                "strict_sessions",
            )
            or False
        )
        # Opt-in: local-NIC/loopback addresses are scopeable targets
        # (default-deny unless enrolled) instead of operator-side filtered.
        local_targets = bool(scope_block.get("local_targets") or False)
        in_pats = _coalesce(scope_block, "in_targets", "in") or []
        out_pats = _coalesce(scope_block, "out_targets", "out") or []
        new_rules: list[ScopeRule] = []
        now = time.time()
        for pat in _as_list(in_pats):
            kind, norm = parse_rule(pat)
            new_rules.append(
                ScopeRule(
                    pattern=norm,
                    kind=kind,
                    direction="in",
                    origin="engagement",
                    added_by="engagement.yaml",
                    added_at=now,
                    reason="",
                )
            )
        for pat in _as_list(out_pats):
            kind, norm = parse_rule(pat)
            new_rules.append(
                ScopeRule(
                    pattern=norm,
                    kind=kind,
                    direction="out",
                    origin="engagement",
                    added_by="engagement.yaml",
                    added_at=now,
                    reason="",
                )
            )
        current = self._snapshot
        generation = current.generation if expected_generation is None else expected_generation
        scope_resource_context = scope_block.get("resource_context") or {}
        scope_credential_bindings = scope_block.get("credential_bindings") or {}
        if not isinstance(scope_credential_bindings, Mapping):
            raise ScopeSnapshotCompatibilityError("credential bindings must be an object")
        snapshot = self._build_snapshot(
            current.generation + 1,
            current.snapshot_id,
            SnapshotDraft(
                tuple(rule for rule in current.rules if rule.origin != "engagement")
                + tuple(new_rules),
                research,
                session_strict,
                json.dumps(scope_resource_context, sort_keys=True, separators=(",", ":")),
                parse_credential_bindings(scope_credential_bindings),
                local_targets,
            ),
        )
        if generation != current.generation:
            raise ScopeSnapshotStaleError("authorization snapshot generation is stale")
        return snapshot

    def publish_snapshot(self, publication: AuthorizationSnapshot) -> AuthorizationSnapshot:
        """Atomically publish a snapshot prepared against the current head."""
        if publication.predecessor_id != self._snapshot.snapshot_id:
            raise ScopeSnapshotStaleError("authorization snapshot predecessor is stale")
        return self._commit_snapshot(publication, self._snapshot.generation)

    def load_engagement_rules(
        self,
        profile: dict[str, Any],
        *,
        expected_generation: int | None = None,
    ) -> AuthorizationSnapshot:
        """Atomically replace engagement-origin rules from the YAML profile."""
        publication = self.prepare_engagement_rules(
            profile,
            expected_generation=expected_generation,
        )
        return self.publish_snapshot(publication)

    def _insert_rule_row(self, r: ScopeRule) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT OR REPLACE INTO scope_rules "
            "(pattern,kind,direction,origin,added_by,added_at,"
            " expires_at,one_shot,consumed_at,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                r.pattern,
                r.kind,
                r.direction,
                r.origin,
                r.added_by,
                r.added_at,
                r.expires_at,
                int(r.one_shot),
                r.consumed_at,
                r.reason,
            ),
        )

    def restore_snapshot(
        self,
        snapshot_id: str,
        *,
        expected_generation: int | None = None,
    ) -> AuthorizationSnapshot:
        if self._conn is None:
            raise ScopeSnapshotCompatibilityError("restore requires persistent scope state")
        row = self._conn.execute(
            "SELECT payload_json FROM scope_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise ScopeSnapshotCompatibilityError("snapshot to restore does not exist")
        source = self._parse_snapshot(row[0], snapshot_id)
        current = self._snapshot
        generation = current.generation if expected_generation is None else expected_generation
        restored = self._build_snapshot(
            current.generation + 1,
            current.snapshot_id,
            SnapshotDraft(
                source.rules,
                source.research,
                source.session_strict,
                source.resource_context_json,
                source.credential_bindings,
                source.local_targets,
            ),
        )
        return self._commit_snapshot(restored, generation)

    # ─── adhoc rule management ───────────────────────────────────────────

    def add_adhoc(
        self,
        pattern: str,
        direction: Direction = "in",
        ttl_seconds: float | None = None,
        one_shot: bool = False,
        reason: str = "",
        added_by: str = "operator",
        force_kind: str | None = None,
    ) -> ScopeRule:
        if not (reason or "").strip():
            raise ValueError("adhoc rules require a non-empty reason")
        kind, norm = parse_rule(pattern, force_kind=force_kind)
        now = time.time()
        rule = ScopeRule(
            pattern=norm,
            kind=kind,
            direction=direction,
            origin="adhoc",
            added_by=added_by,
            added_at=now,
            expires_at=(now + ttl_seconds) if ttl_seconds else None,
            one_shot=one_shot,
            reason=reason,
        )
        current = self._snapshot
        rules = [
            r
            for r in current.rules
            if not (
                r.kind == kind
                and r.pattern == norm
                and r.direction == direction
                and r.origin == "adhoc"
            )
        ]
        rules.append(rule)
        snapshot = self._build_snapshot(
            current.generation + 1,
            current.snapshot_id,
            SnapshotDraft(
                tuple(rules),
                current.research,
                current.session_strict,
                current.resource_context_json,
                current.credential_bindings,
                current.local_targets,
            ),
        )
        self._commit_snapshot(snapshot, current.generation)
        return rule

    def remove(
        self,
        pattern: str,
        direction: Direction = "in",
        force_kind: str | None = None,
    ) -> bool:
        try:
            kind, norm = parse_rule(pattern, force_kind=force_kind)
        except ValueError:
            return False
        current = self._snapshot
        rules = [
            r
            for r in current.rules
            if not (
                r.kind == kind
                and r.pattern == norm
                and r.direction == direction
                and r.origin == "adhoc"
            )
        ]
        removed = len(current.rules) - len(rules)
        if removed:
            snapshot = self._build_snapshot(
                current.generation + 1,
                current.snapshot_id,
                SnapshotDraft(
                    tuple(rules),
                    current.research,
                    current.session_strict,
                    current.resource_context_json,
                    current.credential_bindings,
                    current.local_targets,
                ),
            )
            self._commit_snapshot(snapshot, current.generation)
        return bool(removed)

    def rules(self, include_inactive: bool = False) -> list[ScopeRule]:
        now = time.time()
        snapshot = self._snapshot
        rules = snapshot.rules
        if include_inactive:
            return list(rules)
        return [r for r in rules if r.is_active(now)]

    def has_any_in_rule(self) -> bool:
        return any(r.direction == "in" and r.is_active() for r in self._snapshot.rules)

    def in_scope_origins(self, *, sentinel: str = "https://scope.invalid") -> str:
        """Render the active in-scope rules as a Playwright-style
        `--allowed-origins` value: a ';'-joined list of `scheme://host:*`
        origins. Used to confine a browser-style external MCP server
        (mcp_plugins/browser.yaml + the runner-factory merge step) to the
        engagement at the process level.

        Mapping is lossy — scope is host/IP-based, origins are
        scheme+host+port — so we emit both http+https and any port:
          host_exact  example.internal   -> http://example.internal:*  ; https://example.internal:*
          host_glob   *.example.internal -> http://*.example.internal:* ; https://*.example.internal:*
          network /32 or /128      -> the single host (IPv6 bracketed)
          broader CIDR / wifi      -> skipped (not representable as an origin;
                                      the browser_navigate gate still covers them)

        FAIL-SAFE: if nothing renderable is in scope, returns `sentinel`
        (a non-resolvable origin) so the browser reaches NOTHING until the
        operator sets a host/IP scope — matching the gate's default-deny.
        """
        origins: list[str] = []
        seen: set[str] = set()

        def _add(host: str) -> None:
            for scheme in ("http", "https"):
                o = f"{scheme}://{host}:*"
                if o not in seen:
                    seen.add(o)
                    origins.append(o)

        for r in self.rules(include_inactive=False):
            if r.direction != "in":
                continue
            if r.kind in ("host_exact", "host_glob"):
                _add(r.pattern)
            elif r.kind == "network":
                try:
                    net = ipaddress.ip_network(r.pattern, strict=False)
                except ValueError:
                    continue
                if net.num_addresses == 1:
                    ip = net.network_address
                    _add(f"[{ip}]" if ip.version == 6 else str(ip))
                # broader CIDRs aren't a single browser origin — skip.
            # wifi_* kinds are irrelevant to a browser.
        return ";".join(origins) if origins else sentinel

    # ─── the check ───────────────────────────────────────────────────────

    def check(
        self,
        targets: list[Target],
        relationships: tuple[PrincipalResourceRelationship, ...] = (),
        *,
        relationship_variant: ExternalModeVariant | None = None,
    ) -> CheckResult:
        """Allow IFF every target is in some active 'in' rule AND no
        target is in any active 'out' rule.

        Returns a CheckResult with per-target decisions. The summary
        string is human-readable for the agent's refusal message.
        """
        snapshot = self._snapshot
        result = self._check_snapshot(
            snapshot,
            targets,
            relationships,
            relationship_variant=relationship_variant,
        )
        consumed_oneshots = [
            decision.matched_rule
            for decision in result.decisions
            if decision.matched_rule is not None and decision.matched_rule.one_shot
        ]
        if result.allowed and consumed_oneshots:
            self._consume(snapshot, consumed_oneshots)
        return result

    def _check_snapshot(
        self,
        snapshot: AuthorizationSnapshot,
        targets: list[Target],
        relationships: tuple[PrincipalResourceRelationship, ...] = (),
        *,
        relationship_variant: ExternalModeVariant | None = None,
    ) -> CheckResult:
        rules = snapshot.rules
        binding_errors = tuple(
            (relationship, error)
            for relationship in relationships
            if (
                error := _credential_binding_error(
                    snapshot,
                    relationship,
                    (
                        relationship_variant.credential_binding_required
                        if relationship_variant is not None
                        else False
                    ),
                )
            )
            is not None
        )
        if binding_errors:
            binding_decisions = [
                Decision(
                    target=relationship.principal,
                    verdict="deny",
                    matched_rule=None,
                    reason=error,
                )
                for relationship, error in binding_errors
            ]
            return CheckResult(
                allowed=False,
                decisions=binding_decisions,
                summary="; ".join(error for _, error in binding_errors),
                snapshot_id=snapshot.snapshot_id,
                snapshot_generation=snapshot.generation,
                relationship_denied=True,
            )
        if relationship_variant is not None:
            relationship_error = _validate_relationships(
                relationship_variant,
                ExtractionResult(tuple(targets), relationships),
            )
            if relationship_error is not None:
                denied_targets = tuple(
                    relationship.resource for relationship in relationships
                ) or tuple(targets[:1])
                return CheckResult(
                    allowed=False,
                    decisions=[
                        Decision(
                            target=target,
                            verdict="deny",
                            matched_rule=None,
                            reason=relationship_error,
                        )
                        for target in denied_targets
                    ],
                    summary=relationship_error,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_generation=snapshot.generation,
                    relationship_denied=True,
                )
        if snapshot.local_targets:
            # Opt-in (scope.local_targets): local-NIC/loopback addresses are
            # ordinary targets — evaluated against the rules below, so the
            # default-deny ("engagement has no scope set" / no matching
            # in-rule) applies to loopback labs too.
            op_filtered: list[Target] = []
        else:
            targets, op_filtered = _split_operator_targets(targets)
        op_note = _operator_filter_note(op_filtered)
        if not targets:
            if op_filtered:
                return CheckResult(
                    allowed=True,
                    decisions=[],
                    summary=f"all targets are operator-side ({op_note}) — scope check skipped",
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_generation=snapshot.generation,
                )
            return CheckResult(
                allowed=False,
                decisions=[],
                summary="no targets to check (target extraction returned empty)",
                snapshot_id=snapshot.snapshot_id,
                snapshot_generation=snapshot.generation,
            )
        if not any(rule.direction == "in" and rule.is_active() for rule in rules):
            return CheckResult(
                allowed=False,
                decisions=[],
                summary=(
                    "engagement has no scope set. Run: "
                    "`salientctl prefs set scope.in_targets '[…]'` "
                    "or `salientctl scope add <pattern> --reason '…'` "
                    "before any target-bearing tool can run."
                ),
                snapshot_id=snapshot.snapshot_id,
                snapshot_generation=snapshot.generation,
            )
        decisions: list[Decision] = []
        all_allowed = True
        for t in targets:
            d = self._check_one(t, rules)
            decisions.append(d)
            if d.verdict == "deny":
                all_allowed = False

        summary = self._summarize(decisions, allowed=all_allowed)
        if op_filtered:
            summary = f"{summary} (operator-side filtered: {op_note})"
        return CheckResult(
            allowed=all_allowed,
            decisions=decisions,
            summary=summary,
            snapshot_id=snapshot.snapshot_id,
            snapshot_generation=snapshot.generation,
        )

    def dry_check(
        self,
        targets: list[Target],
        relationships: tuple[PrincipalResourceRelationship, ...] = (),
        *,
        relationship_variant: ExternalModeVariant | None = None,
    ) -> CheckResult:
        """Identical verdict to check() but never mutates state — used
        by read-only callers (e.g. `hosts_suggest`) that need to know
        whether a target would be allowed without consuming one-shot
        rules along the way.
        """
        snapshot = self._current_persisted_snapshot()
        return self._check_snapshot(
            snapshot,
            targets,
            relationships,
            relationship_variant=relationship_variant,
        )

    def pin_snapshot(self) -> AuthorizationSnapshot:
        """Return the latest complete committed authorization snapshot."""
        return self._current_persisted_snapshot()

    def _current_persisted_snapshot(self) -> AuthorizationSnapshot:
        if self._conn is None:
            return self._snapshot
        head = self._conn.execute("SELECT generation FROM scope_head WHERE singleton=1").fetchone()
        if head is None:
            raise ScopeSnapshotCompatibilityError("scope head is missing")
        if head[0] == self._snapshot.generation:
            return self._snapshot
        row = self._conn.execute(
            "SELECT snapshot_id,payload_json FROM scope_snapshots WHERE generation=?",
            (head[0],),
        ).fetchone()
        if row is None:
            raise ScopeSnapshotCompatibilityError("scope head references a missing snapshot")
        return self._parse_snapshot(row[1], row[0])

    def _check_one(self, t: Target, rules: Iterable[ScopeRule]) -> Decision:
        now = time.time()
        # 1) Out rules win.
        for r in rules:
            if r.direction != "out" or not r.is_active(now):
                continue
            if _rule_matches(r, t):
                return Decision(
                    target=t,
                    verdict="deny",
                    matched_rule=r,
                    reason=f"{t.kind} {t.value} matches out-of-scope rule {r.pattern}",
                )
        # 2) In rules.
        for r in rules:
            if r.direction != "in" or not r.is_active(now):
                continue
            if _rule_matches(r, t):
                return Decision(
                    target=t,
                    verdict="allow",
                    matched_rule=r,
                    reason=f"{t.kind} {t.value} matches in-scope rule {r.pattern}",
                )
        # 3) No match → deny.
        return Decision(
            target=t,
            verdict="deny",
            matched_rule=None,
            reason=f"{t.kind} {t.value} is not in any in-scope rule",
        )

    # ─── research lane ─────────────────────────────────────────────────────

    def research_active(self) -> bool:
        """True iff the research lane is enabled (mode != off)."""
        return self._snapshot.research.mode != "off"

    def session_strict(self) -> bool:
        """True iff the engagement opted into per-call scope re-checking of
        relayed session commands (scope.session_strict). When False
        (default), session_scoped tools keep their legacy established-session
        trust bypass. See SC-1 / docs/SCOPE.md."""
        return self._snapshot.session_strict

    def local_targets(self) -> bool:
        """True iff the engagement opted INTO scope-checking local-NIC /
        loopback addresses (scope.local_targets). When False (default),
        local addresses are filtered out as operator-side infrastructure
        before rule evaluation; when True they are ordinary targets —
        default-deny unless enrolled. The research lane is unaffected: its
        public floor fails closed on local addresses either way."""
        return self._snapshot.local_targets

    def research_summary(self) -> dict[str, Any]:
        """Operator-facing snapshot of the research lane policy (for
        `scope list`)."""
        research = self._snapshot.research
        return {
            "mode": research.mode,
            "allowlist": [r.pattern for r in research.in_rules],
            "internal_tlds": list(research.internal_tlds),
        }

    def check_research(
        self,
        targets: list[Target],
        active: bool = False,
    ) -> CheckResult:
        """Verdict for an OSINT/recon (research=True) tool. ADDITIVE to the
        engagement scope: a target is allowed iff it's already in engagement
        scope OR it passes the public floor (globally-routable, not internal,
        not in `out_targets`). Never requires `has_any_in_rule`, so pure-OSINT
        runs work with empty engagement scope. Operator-side targets are NOT
        split out here — the floor denies local-NIC addresses itself.

        `active` (= the tool's `ExtractorSpec.research_active`): when True the
        floor fails CLOSED on a hostname that doesn't resolve (an active probe
        could still reach it via split-horizon DNS). Passive tools fail open."""
        if not targets:
            return CheckResult(
                allowed=False,
                decisions=[],
                summary="no targets to check (extraction returned empty)",
            )
        # Snapshot the rule list once: check_research runs off the event loop
        # (gate() dispatches it onto a dedicated executor so a blocking DNS
        # lookup doesn't stall the daemon), and the loop thread can mutate
        # _rules concurrently (operator scope add). Iterating a snapshot
        # avoids the "list changed size during iteration" race.
        snapshot = self._snapshot
        rules = snapshot.rules
        decisions = [self._check_research_one(t, rules, snapshot.research, active) for t in targets]
        all_allowed = all(d.verdict == "allow" for d in decisions)
        return CheckResult(
            allowed=all_allowed,
            decisions=decisions,
            summary=self._summarize(decisions, allowed=all_allowed),
        )

    def _check_research_one(
        self,
        t: Target,
        rules: Iterable[ScopeRule],
        research: ResearchPolicy,
        active: bool = False,
    ) -> Decision:
        now = time.time()
        # 1) Operator denylist always wins (same as the strict lane).
        for r in rules:
            if r.direction == "out" and r.is_active(now) and _rule_matches(r, t):
                return Decision(
                    target=t,
                    verdict="deny",
                    matched_rule=r,
                    reason=f"{t.kind} {t.value} matches out-of-scope rule {r.pattern}",
                )
        # 2) Already in engagement scope → allow (the operator opted in; this
        #    is what makes the lane purely additive — it never removes a grant).
        for r in rules:
            if r.direction == "in" and r.is_active(now) and _rule_matches(r, t):
                return Decision(
                    target=t,
                    verdict="allow",
                    matched_rule=r,
                    reason=f"{t.kind} {t.value} in engagement scope ({r.pattern})",
                )
        # 3) Research public branch.
        if research.mode == "off":
            return Decision(
                target=t,
                verdict="deny",
                matched_rule=None,
                reason=f"{t.kind} {t.value} not in engagement scope (research lane off)",
            )
        if research.mode == "allowlist":
            matched = next((r for r in research.in_rules if _rule_matches(r, t)), None)
            if matched is None:
                return Decision(
                    target=t,
                    verdict="deny",
                    matched_rule=None,
                    reason=f"{t.kind} {t.value} not in the research allowlist",
                )
        ok, reason = self._research_public_floor(t, research, active)
        if not ok:
            return Decision(
                target=t,
                verdict="deny",
                matched_rule=None,
                reason=reason,
            )
        return Decision(
            target=t,
            verdict="allow",
            matched_rule=None,
            reason=f"{t.kind} {t.value} allowed via research lane (public)",
        )

    def _research_public_floor(
        self,
        t: Target,
        research: ResearchPolicy,
        active: bool = False,
    ) -> tuple[bool, str]:
        """The load-bearing safety floor: a research target may only be a
        genuinely PUBLIC host. Denies private/internal IPs, internal-TLD
        hostnames, and hostnames that resolve to a non-global address.

        On a hostname that does not resolve, `active` decides the posture:
        active resolve-and-touch tools fail CLOSED (can't verify public, and
        split-horizon DNS could still reach it); passive DB-lookup tools fail
        OPEN (they only ever query public databases, never the host)."""
        if t.kind == "ip":
            try:
                ip = ipaddress.ip_address(t.value)
            except ValueError:
                return False, f"unparseable IP {t.value}"
            if not _ip_is_global(ip):
                return False, (
                    f"{t.value} is a private/internal address — the research "
                    f"lane is public-only (add it to engagement scope to reach it)"
                )
            return True, ""
        if t.kind in ("host", "url"):
            host = t.value.lower().rstrip(".")
            for tld in research.internal_tlds:
                if host == tld.lstrip(".") or host.endswith(tld):
                    return False, (
                        f"{host} is in an internal namespace ({tld}) — the "
                        f"research lane denies internal hosts"
                    )
            addrs = _resolve_cached(host)
            if addrs is None:
                if active:
                    # Active probe + can't verify public → fail CLOSED. A
                    # split-horizon resolver could still reach an internal host.
                    return False, (
                        f"{host} did not resolve — an active research probe "
                        f"can't verify it's public, so the lane denies it "
                        f"(add it to engagement scope to reach it)"
                    )
                # Passive DB lookup never touches the host → fail open.
                return True, ""
            for a in addrs:
                if not _ip_is_global(a):
                    return False, (
                        f"{host} resolves to non-global {a} — the research "
                        f"lane denies internal/private infrastructure"
                    )
            return True, ""
        return False, (f"{t.kind} {t.value} is not eligible for the research lane")

    def _consume(
        self,
        pinned: AuthorizationSnapshot,
        rules: Iterable[ScopeRule],
    ) -> None:
        now = time.time()
        identities = {
            (rule.kind, rule.pattern, rule.direction, rule.origin, rule.added_at) for rule in rules
        }
        updated = tuple(
            replace(rule, consumed_at=now)
            if (rule.kind, rule.pattern, rule.direction, rule.origin, rule.added_at) in identities
            else rule
            for rule in pinned.rules
        )
        snapshot = self._build_snapshot(
            pinned.generation + 1,
            pinned.snapshot_id,
            SnapshotDraft(
                updated,
                pinned.research,
                pinned.session_strict,
                pinned.resource_context_json,
                pinned.credential_bindings,
                pinned.local_targets,
            ),
        )
        self._commit_snapshot(snapshot, pinned.generation)

    def _summarize(self, decisions: list[Decision], allowed: bool) -> str:
        if allowed:
            return "; ".join(d.reason for d in decisions)
        denies = [d for d in decisions if d.verdict == "deny"]
        if not denies:
            return "denied (no decisions)"
        return "; ".join(d.reason for d in denies)

    # ─── decision logging ────────────────────────────────────────────────

    def authorize_and_log(
        self,
        invocation: ToolInvocation,
        audit_args: dict[str, Any],
        targets: tuple[Target, ...],
        relationships: tuple[PrincipalResourceRelationship, ...] = (),
        *,
        relationship_variant: ExternalModeVariant | None = None,
        correlation_id: str | None = None,
        decision_id: str | None = None,
    ) -> CheckResult:
        if self._conn is None:
            return self.check(
                list(targets),
                relationships,
                relationship_variant=relationship_variant,
            )
        self._conn.execute("BEGIN IMMEDIATE")
        pinned = self._snapshot
        try:
            head = self._conn.execute(
                "SELECT generation FROM scope_head WHERE singleton=1"
            ).fetchone()
            if head is None:
                raise ScopeSnapshotCompatibilityError("scope head is missing")
            row = self._conn.execute(
                "SELECT snapshot_id,payload_json FROM scope_snapshots WHERE generation=?",
                (head[0],),
            ).fetchone()
            if row is None:
                raise ScopeSnapshotCompatibilityError("scope head references a missing snapshot")
            pinned = self._parse_snapshot(row[1], row[0])
            result = self._check_snapshot(
                pinned,
                list(targets),
                relationships,
                relationship_variant=relationship_variant,
            )
            from .scope_audit import scope_audit

            audit = scope_audit(invocation, targets, result, relationships)
            self._insert_decision(
                agent=invocation.agent_id,
                tool=invocation.wire_name,
                args=audit_args,
                targets=list(audit.targets),
                relationships=list(audit.relationships),
                result=audit.result,
                correlation_id=correlation_id,
                decision_id=decision_id,
            )
            consumed = [
                decision.matched_rule
                for decision in result.decisions
                if decision.matched_rule is not None and decision.matched_rule.one_shot
            ]
            published = pinned
            if result.allowed and consumed:
                now = time.time()
                identities = {
                    (rule.kind, rule.pattern, rule.direction, rule.origin, rule.added_at)
                    for rule in consumed
                }
                updated = tuple(
                    replace(rule, consumed_at=now)
                    if (rule.kind, rule.pattern, rule.direction, rule.origin, rule.added_at)
                    in identities
                    else rule
                    for rule in pinned.rules
                )
                published = self._build_snapshot(
                    pinned.generation + 1,
                    pinned.snapshot_id,
                    SnapshotDraft(
                        updated,
                        pinned.research,
                        pinned.session_strict,
                        pinned.resource_context_json,
                        pinned.credential_bindings,
                        pinned.local_targets,
                    ),
                )
                self._insert_snapshot_row(published)
                self._conn.execute("DELETE FROM scope_rules")
                for rule in published.rules:
                    self._insert_rule_row(rule)
                changed = self._conn.execute(
                    "UPDATE scope_head SET generation=? WHERE singleton=1 AND generation=?",
                    (published.generation, pinned.generation),
                )
                if changed.rowcount != 1:
                    raise ScopeSnapshotStaleError("authorization snapshot generation is stale")
            self._conn.execute("COMMIT")
        except sqlite3.Error:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            return CheckResult(
                allowed=False,
                decisions=[],
                summary="audit persistence failed; authorization denied",
                snapshot_id=pinned.snapshot_id,
                snapshot_generation=pinned.generation,
            )
        except ScopeSnapshotError:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        except BaseException:  # noqa: BLE001 — roll back, then re-raise unchanged
            # Interrupt-safe: a KeyboardInterrupt / CancelledError rolls back and
            # propagates — it must NOT be swallowed into a deny CheckResult.
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        self._publish_snapshot(published)
        return result

    def _insert_decision(
        self,
        agent: str,
        tool: str,
        args: dict[str, Any],
        targets: list[Target],
        relationships: list[PrincipalResourceRelationship],
        result: CheckResult,
        correlation_id: str | None = None,
        decision_id: str | None = None,
    ) -> None:
        assert self._conn is not None
        decisions = [
            {
                "reason": decision.reason,
                "rule_id": (
                    stable_rule_id(decision.matched_rule)
                    if decision.matched_rule is not None
                    else None
                ),
                "target": dataclasses.asdict(decision.target),
                "verdict": decision.verdict,
            }
            for decision in result.decisions
        ]
        self._conn.execute(
            "INSERT INTO scope_decisions "
            "(ts,engagement_id,agent,tool,args_json,targets_json,verdict,matched_rule,reason,"
            " decisions_json,relationships_json,rule_ids_json,snapshot_id,generation,correlation_id,"
            " decision_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                self.engagement_id,
                agent,
                tool,
                _json_dumps(args),
                _json_dumps([dataclasses.asdict(target) for target in targets]),
                "allow" if result.allowed else "deny",
                _matched_pattern(result),
                result.summary,
                _json_dumps(decisions),
                _json_dumps([dataclasses.asdict(item) for item in relationships]),
                _json_dumps(list(result.rule_ids)),
                result.snapshot_id,
                result.snapshot_generation,
                correlation_id,
                decision_id,
            ),
        )

    def log_decision(
        self,
        agent: str,
        tool: str,
        args: dict[str, Any],
        targets: list[Target],
        relationships: list[PrincipalResourceRelationship],
        result: CheckResult,
        correlation_id: str | None = None,
        decision_id: str | None = None,
    ) -> CheckResult:
        if self._conn is None:
            return replace(
                result,
                snapshot_id=self._snapshot.snapshot_id,
                snapshot_generation=self._snapshot.generation,
            )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            pinned = self._current_persisted_snapshot()
            pinned_result = replace(
                result,
                snapshot_id=pinned.snapshot_id,
                snapshot_generation=pinned.generation,
            )
            self._insert_decision(
                agent,
                tool,
                args,
                targets,
                relationships,
                pinned_result,
                correlation_id=correlation_id,
                decision_id=decision_id,
            )
            self._conn.execute("COMMIT")
        except BaseException:  # noqa: BLE001 — roll back, then re-raise unchanged
            # Interrupt-safe (see restore_checkpoint): never leak an open
            # transaction, even on KeyboardInterrupt / CancelledError.
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        self._publish_snapshot(pinned)
        return pinned_result

    def deny_log(
        self,
        since: float | None = None,
        agent: str | None = None,
        tool: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        clauses = ["verdict='deny'"]
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if tool:
            clauses.append("tool = ?")
            params.append(tool)
        where = " AND ".join(clauses)
        cur = self._conn.execute(
            f"SELECT ts,agent,tool,args_json,targets_json,reason,matched_rule "
            f"FROM scope_decisions WHERE {where} "
            f"ORDER BY ts DESC LIMIT ?",
            (*params, limit),
        )
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            ts, agent_, tool_, args_json, targets_json, reason, matched = row
            out.append(
                {
                    "ts": ts,
                    "agent": agent_,
                    "tool": tool_,
                    "args": _json_loads(args_json),
                    "targets": _json_loads(targets_json),
                    "reason": reason,
                    "matched_rule": matched,
                }
            )
        return out

    def scope_denies_for_correlation(
        self, correlation_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """T3.1 spine: the authoritative scope DENY rows for one correlation id —
        the side of the audit_mirror cross-check reconstruct trusts. A deny here
        that is missing from the mirror means the chain is INCOMPLETE (the mirror
        is best-effort; scope.db is fail-closed authoritative). Empty without a
        DB."""
        if self._conn is None:
            return []
        cur = self._conn.execute(
            "SELECT ts,agent,tool,targets_json,reason,decision_id FROM scope_decisions "
            "WHERE verdict='deny' AND correlation_id=? ORDER BY ts LIMIT ?",
            (correlation_id, max(1, min(int(limit), 5000))),
        )
        return [
            {
                "ts": r[0],
                "agent": r[1],
                "tool": r[2],
                "targets": _json_loads(r[3]),
                "reason": r[4],
                # T3.1 H1: the per-row twin key (the mirror's tool_use_id). NULL
                # on pre-migration rows — reconstruct falls back to the weak
                # (agent,tool) multiset and discloses the weaker tier when so.
                "decision_id": r[5],
            }
            for r in cur.fetchall()
        ]

    def counts(self) -> dict[str, int]:
        """Aggregate (allow, deny) counts for sitrep / salient-report."""
        if self._conn is None:
            return {"allow": 0, "deny": 0}
        cur = self._conn.execute(
            "SELECT verdict, COUNT(*) FROM scope_decisions WHERE engagement_id=? GROUP BY verdict",
            (self.engagement_id,),
        )
        out = {"allow": 0, "deny": 0}
        for verdict, n in cur.fetchall():
            out[verdict] = n
        return out

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# ─── per-turn prompt injection ─────────────────────────────────────────────


def render_scope_block(
    store: ScopeStore | None,
    *,
    max_rules: int = 50,
) -> str:
    """Render the current active scope as a compact block to prepend to
    every agent task message.

    The agents repeatedly asked the operator to confirm scope even after
    it was set — the YAML profile gets stale relative to mid-engagement
    `scope add` operations, and operator answers in the inbox don't
    persist into later turns. This block surfaces the *authoritative*
    store state on every dispatch so the agent can see what is already
    authorized.

    Returns "" when there is nothing to inject (no store wired). Caller
    decides whether to skip rendering or emit a no-scope warning.
    """
    if store is None:
        return ""
    active = list(store.rules(include_inactive=False))
    in_eng = sorted({r.pattern for r in active if r.direction == "in" and r.origin == "engagement"})
    in_adhoc = sorted({r.pattern for r in active if r.direction == "in" and r.origin == "adhoc"})
    out_eng = sorted(
        {r.pattern for r in active if r.direction == "out" and r.origin == "engagement"}
    )
    out_adhoc = sorted({r.pattern for r in active if r.direction == "out" and r.origin == "adhoc"})

    if not (in_eng or in_adhoc or out_eng or out_adhoc):
        return (
            "Active engagement scope: NONE SET.\n"
            "Every target-bearing tool call will be REFUSED until the "
            "operator runs `salientctl prefs set scope.in_targets '[…]'` "
            "or `salientctl scope add …`. Do not re-ask the operator "
            "about scope — they are aware; report findings unrelated to "
            "tool execution and wait for scope to be set."
        )

    def _join(rules: list[str]) -> str:
        if len(rules) <= max_rules:
            return ", ".join(rules)
        head = ", ".join(rules[:max_rules])
        return f"{head}, (+{len(rules) - max_rules} more)"

    lines = [
        "Active engagement scope (authoritative; updated only by `salientctl scope add/remove`):"
    ]
    if in_eng:
        lines.append(f"  Authorized (engagement.yaml): {_join(in_eng)}")
    if in_adhoc:
        lines.append(f"  Authorized (adhoc, current run): {_join(in_adhoc)}")
    if out_eng:
        lines.append(f"  Denied (engagement.yaml): {_join(out_eng)}")
    if out_adhoc:
        lines.append(f"  Denied (adhoc): {_join(out_adhoc)}")
    lines.append("")
    lines.append(
        "The scope gate enforces this list deterministically at every "
        "tool call. Treat it as already-answered: do NOT file "
        '`<ask_operator>` questions of the form "is X in scope?" or '
        '"can I scan Y?" when X/Y is already listed above — proceed. '
        "Do NOT propose `salientctl scope add` to the operator as a "
        "workaround for a REFUSED call; if a target is genuinely "
        "required for the current task and not listed, file ONE "
        "`<ask_operator>` stating the target and why it is needed, "
        "then stop."
    )
    return "\n".join(lines)


# ─── rule-match logic ──────────────────────────────────────────────────────


def _rule_matches(rule: ScopeRule, target: Target) -> bool:
    """Does this rule cover this target?

    Semantics:
      network rule, ip target      → ip ∈ network
      network rule, network target → target ⊆ rule
      network rule, host target    → False (no DNS resolution in v1)

      host_exact rule, host target → equal (after IDNA normalize)
      host_exact rule, ip target   → False

      host_glob rule (`*.suffix`)  → target.host == suffix OR
                                     target.host endswith ".suffix"
      host_glob rule, ip target    → False
    """
    if rule.kind in {"repo", "cloud", "saas"}:
        return target.kind == rule.kind and target.value == rule.pattern

    if rule.kind == "network":
        try:
            net = ipaddress.ip_network(rule.pattern, strict=False)
        except ValueError:
            return False
        if target.kind == "ip":
            try:
                return ipaddress.ip_address(target.value) in net
            except ValueError:
                return False
        if target.kind == "network":
            try:
                tn = ipaddress.ip_network(target.value, strict=False)
                # tn/net are both IPv4Network|IPv6Network; subnet_of's stubs
                # require a matching concrete type. A v4/v6 mismatch raises
                # TypeError at runtime, which the caller does not expect here —
                # but ip_network parsing keeps them the same family in practice.
                return tn.subnet_of(net)  # type: ignore[arg-type]
            except ValueError:
                return False
        return False

    if rule.kind == "host_exact":
        if target.kind != "host":
            return False
        return rule.pattern.rstrip(".") == target.value.rstrip(".")

    if rule.kind == "host_glob":
        if target.kind != "host":
            return False
        if not rule.pattern.startswith("*."):
            return False
        suffix = rule.pattern[2:].rstrip(".")
        v = target.value.rstrip(".")
        return v == suffix or v.endswith("." + suffix)

    if rule.kind == "wifi_bssid":
        if target.kind != "wifi_bssid":
            return False
        # Both stored canonical (uppercase + colons), but normalize defensively.
        try:
            return _normalize_bssid(rule.pattern) == _normalize_bssid(target.value)
        except ValueError:
            return False

    if rule.kind == "wifi_ssid":
        if target.kind != "wifi_ssid":
            return False
        return rule.pattern == target.value  # case-sensitive per 802.11

    return False


# ─── the gate (wraps an SdkMcpTool) ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ScopeGatedHandler:
    identity: InvocationIdentity
    store: ScopeStore
    dataset: PolicyDataset
    original: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

    async def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        invocation = ToolInvocation.normalize(self.identity, args)
        evaluation = await evaluate_scope(invocation, self.store, self.dataset)
        if not evaluation.allowed:
            return _refused(evaluation.reason)
        return await self.original(args)


def gate(
    sdk_tool: Any,  # claude_agent_sdk.SdkMcpTool — duck-typed to avoid hard dep
    wire_name: str,
    agent_name: str,
    store: ScopeStore,
    tool_type: str | None = None,
    *,
    dataset: PolicyDataset | None = None,
) -> Any:
    """Wrap an SdkMcpTool's handler with the scope-enforcement gate.

    Returns a new SdkMcpTool (via dataclasses.replace) with the same
    name/description/schema/annotations and a wrapped handler that:

      1. Looks up the extractor spec by `tool_type.wire_name` (specific)
         falling back to `wire_name` (shared default). If neither is
         present → fail-closed (refused with "unclassified tool").
      2. If local_only → log allow, call original handler.
      3. If none → call original handler (no logging, no check).
      4. Otherwise: extract_targets(spec, args).
         If extraction raises → deny with the parser's reason.
         Else: store.check(targets). Log decision.
         If deny → return REFUSED text. Else: call original handler.

    The (tool_type.wire_name) key disambiguates a wire name shared across
    factories — e.g. several factories may each expose a "scan" tool with
    different field shapes; each gets its own spec via "<type>.scan". Generic
    fallback by wire_name covers the truly-uniform cases ("run" → raw_argv
    everywhere).
    """
    from .registry import get_active

    if isinstance(sdk_tool.handler, _ScopeGatedHandler):
        return sdk_tool

    qualified_name = f"{tool_type}.{wire_name}" if tool_type else wire_name
    identity = InvocationIdentity(
        transport=InvocationTransport.MCP,
        wire_name=wire_name,
        qualified_name=qualified_name,
        agent_id=agent_name,
    )
    active_dataset = dataset or get_active()
    return replace(
        sdk_tool,
        handler=_ScopeGatedHandler(
            identity=identity,
            store=store,
            dataset=active_dataset,
            original=sdk_tool.handler,
        ),
    )


def _refused(reason: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"REFUSED (scope): {reason}"}],
        "is_error": True,
    }


# ─── helpers ───────────────────────────────────────────────────────────────


def _as_list(x: Any) -> list[str]:
    if x is None:
        return []
    if isinstance(x, str):
        return [x]
    if isinstance(x, list):
        return [str(s) for s in x]
    return [str(x)]


def _coalesce(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str, sort_keys=False)
    except (TypeError, ValueError):
        return json.dumps({"_unrepresentable": repr(obj)})


def _json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return s


def _matched_pattern(result: CheckResult) -> str | None:
    for d in result.decisions:
        if d.matched_rule is not None:
            return d.matched_rule.pattern
    return None


# ─── extractor table (which tool fields hold targets) ──────────────────────
#
# Keys are qualified SDK names or MCP/bus compatibility wire names. A tool
# absent from the active dataset fails CLOSED, so the kernel's GENERIC default
# covers only its known SDK schemas, built-in bus tools, and generic networking
# examples. A downstream skin swaps in its domain-specific taxonomy via
# `registry.set_active(PolicyDataset(tool_targets=...))`.
_DEFAULT_TOOL_TARGETS: dict[str, ExtractorSpec] = {
    "builtin.Bash": ExtractorSpec(fields={"command": "raw_argv"}),
    "builtin.Read": ExtractorSpec(local_only=True),
    "builtin.Grep": ExtractorSpec(local_only=True),
    "builtin.Glob": ExtractorSpec(local_only=True),
    "builtin.Write": ExtractorSpec(local_only=True),
    "builtin.Edit": ExtractorSpec(local_only=True),
    "builtin.Agent": ExtractorSpec(none=True),
    "builtin.Task": ExtractorSpec(none=True),
    # Built-in bus tools — never scope-checked (in-process, no remote target).
    # Kept in sync with salient_core.bus._BUS_TOOL_NAMES (minus the domain
    # tools a skin supplies). Listed literally to avoid a policy->bus import.
    "ask_agent": ExtractorSpec(none=True),
    "ask_agents": ExtractorSpec(none=True),
    "ask_consensus": ExtractorSpec(none=True),
    "ask_operator": ExtractorSpec(none=True),
    "ask_partner": ExtractorSpec(none=True),
    "context_count": ExtractorSpec(none=True),
    "context_grep": ExtractorSpec(none=True),
    "context_head": ExtractorSpec(none=True),
    "context_lines": ExtractorSpec(none=True),
    "context_list": ExtractorSpec(none=True),
    "context_read": ExtractorSpec(none=True),
    "context_section": ExtractorSpec(none=True),
    "context_summary": ExtractorSpec(none=True),
    "context_tail": ExtractorSpec(none=True),
    "context_write": ExtractorSpec(none=True),
    "get_skill": ExtractorSpec(none=True),
    "kg_assert": ExtractorSpec(none=True),
    "kg_neighbors": ExtractorSpec(none=True),
    "kg_query": ExtractorSpec(none=True),
    "kg_semantic_query": ExtractorSpec(none=True),
    "kg_stats": ExtractorSpec(none=True),
    "list_agents": ExtractorSpec(none=True),
    "prior_actions": ExtractorSpec(none=True),
    "propose_lesson": ExtractorSpec(none=True),
    "propose_skill": ExtractorSpec(none=True),
    "read_evidence": ExtractorSpec(none=True),
    "record_review": ExtractorSpec(none=True),
    "rule_validate": ExtractorSpec(none=True),
    "search_skills": ExtractorSpec(none=True),
    "spawn_template": ExtractorSpec(none=True),
    "swarm_finish": ExtractorSpec(none=True),
    # Generic networking examples so the gate is exercised standalone.
    "http_get": ExtractorSpec(fields={"url": "url"}),
    "curl": ExtractorSpec(fields={"target": "url_or_host"}),
    "ssh": ExtractorSpec(fields={"host": "host"}),
    "ping": ExtractorSpec(fields={"target": "ip_or_host"}),
    "subnet_probe": ExtractorSpec(fields={"cidrs": "cidr_list"}),
    "run": ExtractorSpec(fields={"command": "raw_argv"}),
    "local_task": ExtractorSpec(local_only=True),
}

# Tools wired into TOOL_TARGETS so far cover the demo path (scanner,
# subdomain, bash, web fetch, generic-scan-by-URL, bus tools).
#
# Adding the rest is mechanical and is tracked in docs/SCOPE.md. Until a
# wire name has an entry here, the gate refuses calls to it with
# "unclassified tool — fail-closed."
#
# This is by design: when you add a new tool to tools.py, the daemon
# will refuse to use it on the first attempt and tell you exactly what
# to do — add a TOOL_TARGETS entry. You can't accidentally ship a new
# tool that's exempt from scope enforcement.


def __getattr__(name: str) -> Any:
    # Tombstone the relocated public constant: the extractor table is now the
    # injectable ``PolicyDataset.tool_targets`` (see policy.registry). Any
    # lingering ``from ...scope import TOOL_TARGETS`` fails loudly here rather
    # than silently binding stale/generic data in the default-deny gate.
    if name == "TOOL_TARGETS":
        raise AttributeError(
            "TOOL_TARGETS was replaced by the injectable policy dataset — "
            "read policy.registry.get_active().tool_targets, or register your "
            "own via policy.registry.set_active(PolicyDataset(...)). The kernel "
            "default lives in policy.defaults.DEFAULT_DATASET."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
