"""Canonical literal identities for repository, cloud, and SaaS resources."""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Final, Literal, TypeGuard, assert_never

ResourceKind = Literal["repo", "cloud", "saas"]
CloudProvider = Literal["aws", "azure", "gcp"]
SaasIdentityType = Literal["username", "email"]

_RESOURCE_TAG: Final = re.compile(r"^(?P<family>[A-Za-z]+):(?P<body>.*)$", re.ASCII)
_ESCAPE: Final = re.compile(r"%[0-9A-Fa-f]{2}")
_TOKEN: Final = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", re.ASCII)
_HOST: Final = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.ASCII,
)
_AWS_ACCOUNT: Final = re.compile(r"^[0-9]{12}$", re.ASCII)
_KNOWN_CLOUD_PROVIDERS: Final = frozenset({"aws", "azure", "gcp"})
_RESOURCE_FAMILIES: Final = frozenset({"repo", "cloud", "saas"})


@dataclass(frozen=True, slots=True)
class ResourceIdentityError(ValueError):
    """A tagged resource identity cannot be interpreted canonically."""

    pattern: str
    reason: str

    def __str__(self) -> str:
        return f"invalid resource identity {self.pattern!r}: {self.reason}"


def looks_tagged(pattern: str) -> bool:
    """Return whether text uses a resource-like alphabetic family tag."""
    return _RESOURCE_TAG.match(pattern.strip()) is not None


def _is_resource_kind(value: str) -> TypeGuard[ResourceKind]:
    return value in _RESOURCE_FAMILIES


def _is_cloud_provider(value: str) -> TypeGuard[CloudProvider]:
    return value in _KNOWN_CLOUD_PROVIDERS


def _is_saas_identity_type(value: str) -> TypeGuard[SaasIdentityType]:
    return value in {"username", "email"}


def parse_resource_identity(pattern: str) -> tuple[ResourceKind, str]:
    """Parse one tagged authoring string into its sole canonical representation."""
    authored = pattern.strip()
    match = _RESOURCE_TAG.fullmatch(authored)
    if match is None:
        raise ResourceIdentityError(pattern, "expected repo:, cloud:, or saas: family tag")
    family = match.group("family")
    if not _is_resource_kind(family):
        raise ResourceIdentityError(pattern, f"unknown family {family!r}")
    body = match.group("body")
    match family:
        case "repo":
            return "repo", f"repo:{_parse_repo(pattern, body)}"
        case "cloud":
            return "cloud", f"cloud:{_parse_cloud(pattern, body)}"
        case "saas":
            return "saas", f"saas:{_parse_saas(pattern, body)}"
        case unreachable:
            assert_never(unreachable)


def _decode_segment(pattern: str, raw: str) -> str:
    if not raw:
        raise ResourceIdentityError(pattern, "empty segments are not allowed")
    cursor = 0
    while cursor < len(raw):
        if raw[cursor] == "%":
            if _ESCAPE.match(raw, cursor) is None:
                raise ResourceIdentityError(pattern, f"malformed percent escape in {raw!r}")
            cursor += 3
        else:
            cursor += 1
    try:
        decoded = urllib.parse.unquote_to_bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResourceIdentityError(pattern, f"segment {raw!r} is not UTF-8") from exc
    normalized = unicodedata.normalize("NFC", decoded)
    if not normalized:
        raise ResourceIdentityError(pattern, "empty segments are not allowed")
    if "*" in normalized:
        raise ResourceIdentityError(pattern, "wildcards are not allowed in resource identities")
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in normalized):
        raise ResourceIdentityError(pattern, "control and format characters are not allowed")
    return normalized


def _encode_segment(pattern: str, raw: str) -> str:
    decoded = _decode_segment(pattern, raw)
    return urllib.parse.quote(decoded, safe="-._~", encoding="utf-8", errors="strict")


def _ascii_token(pattern: str, raw: str, label: str) -> str:
    decoded = _decode_segment(pattern, raw).lower()
    if _TOKEN.fullmatch(decoded) is None:
        raise ResourceIdentityError(pattern, f"{label} must be a literal ASCII token")
    return decoded


def _host(pattern: str, raw: str) -> str:
    decoded = _decode_segment(pattern, raw).rstrip(".")
    try:
        canonical = decoded.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ResourceIdentityError(pattern, f"invalid repository host {decoded!r}") from exc
    if _HOST.fullmatch(canonical) is None:
        raise ResourceIdentityError(pattern, f"invalid repository host {decoded!r}")
    return canonical


def _parse_repo(pattern: str, body: str) -> str:
    raw_segments = body.split("/")
    if len(raw_segments) != 3:
        raise ResourceIdentityError(pattern, "repo requires host/owner/repository")
    host, owner, repository = raw_segments
    return "/".join(
        (
            _host(pattern, host),
            _encode_segment(pattern, owner),
            _encode_segment(pattern, repository),
        )
    )


def _parse_cloud(pattern: str, body: str) -> str:
    raw_segments = body.split("/")
    if not raw_segments:
        raise ResourceIdentityError(pattern, "cloud requires a provider")
    provider = _ascii_token(pattern, raw_segments[0], "cloud provider")
    if not _is_cloud_provider(provider):
        raise ResourceIdentityError(pattern, f"unknown cloud provider {provider!r}")
    match provider:
        case "aws":
            if len(raw_segments) < 6:
                raise ResourceIdentityError(
                    pattern, "AWS requires partition/service/region/account/resource"
                )
            partition = _ascii_token(pattern, raw_segments[1], "AWS partition")
            service = _ascii_token(pattern, raw_segments[2], "AWS service")
            region = _ascii_token(pattern, raw_segments[3], "AWS region")
            account = _decode_segment(pattern, raw_segments[4])
            if _AWS_ACCOUNT.fullmatch(account) is None:
                raise ResourceIdentityError(pattern, "AWS account must contain exactly 12 digits")
            resource = [_encode_segment(pattern, value) for value in raw_segments[5:]]
            return "/".join((provider, partition, service, region, account, *resource))
        case "azure":
            if len(raw_segments) < 4:
                raise ResourceIdentityError(pattern, "Azure requires tenant/subscription/resource")
        case "gcp":
            if len(raw_segments) < 4:
                raise ResourceIdentityError(pattern, "GCP requires organization/project/resource")
        case unreachable:
            assert_never(unreachable)
    return "/".join((provider, *(_encode_segment(pattern, value) for value in raw_segments[1:])))


def _parse_saas(pattern: str, body: str) -> str:
    raw_segments = body.split("/")
    if len(raw_segments) < 3:
        raise ResourceIdentityError(pattern, "SaaS requires platform/id-type/principal")
    platform = _ascii_token(pattern, raw_segments[0], "SaaS platform")
    identity_type = _ascii_token(pattern, raw_segments[1], "SaaS identity type")
    if not _is_saas_identity_type(identity_type):
        raise ResourceIdentityError(pattern, f"unknown SaaS identity type {identity_type!r}")
    match identity_type:
        case "username":
            if len(raw_segments) != 3:
                raise ResourceIdentityError(pattern, "username identity requires one principal")
            principal = _decode_segment(pattern, raw_segments[2])
            if platform in {"github", "google"}:
                principal = principal.lower()
            return "/".join((platform, identity_type, urllib.parse.quote(principal, safe="-._~")))
        case "email":
            if len(raw_segments) != 4:
                raise ResourceIdentityError(pattern, "email identity requires local/domain")
            local = _decode_segment(pattern, raw_segments[2])
            if platform in {"google", "microsoft"}:
                local = local.lower()
            domain = _host(pattern, raw_segments[3])
            return "/".join(
                (platform, identity_type, urllib.parse.quote(local, safe="-._~"), domain)
            )
        case unreachable:
            assert_never(unreachable)
