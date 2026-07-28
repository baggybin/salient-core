"""Stable public facade for downstream scope extractors (SCOPE_API_VERSION).

A skin's registered extractor kinds (see `scope.register_extractor`) import
these names instead of reaching into the kernel's `_`-prefixed internals, so the
kernel can refactor those internals freely as long as this surface holds. Bump
`SCOPE_API_VERSION` when this facade changes in a way a skin must adapt to; a
skin asserts compatibility at startup.
"""

from __future__ import annotations

from dataclasses import dataclass

from .scope import (
    SCOPE_API_VERSION,
    CrossTenantGrant,
    ExternalModeContract,
    ExternalModeContractError,
    ExternalModeVariant,
    ExtractionResult,
    ExtractorCtx,
    ExtractorError,
    ExtractorSpec,
    PrincipalResourceRelationship,
    PrincipalResourceRequirement,
    ProviderTargetBinding,
    Target,
    TargetIdentity,
    register_extractor,
)

# Generic extraction primitives a domain extractor composes over.
from .scope import _classify_token as classify_token
from .scope import _is_obfuscated as is_obfuscated
from .scope import _sweep_tokens as sweep_tokens


@dataclass(frozen=True, slots=True)
class ScopeApiVersionError(RuntimeError):
    """A downstream skin expects an incompatible scope extraction API."""

    expected: int
    actual: int

    def __str__(self) -> str:
        return f"scope API mismatch: expected {self.expected}, kernel provides {self.actual}"


def require_scope_api_version(expected: int) -> None:
    """Fail startup when a downstream skin targets another scope API version."""
    if expected != SCOPE_API_VERSION:
        raise ScopeApiVersionError(expected=expected, actual=SCOPE_API_VERSION)


__all__ = [
    "SCOPE_API_VERSION",
    "ScopeApiVersionError",
    "CrossTenantGrant",
    "ExternalModeContract",
    "ExternalModeContractError",
    "ExternalModeVariant",
    "ExtractionResult",
    "ExtractorCtx",
    "ExtractorError",
    "ExtractorSpec",
    "PrincipalResourceRelationship",
    "PrincipalResourceRequirement",
    "ProviderTargetBinding",
    "Target",
    "TargetIdentity",
    "classify_token",
    "is_obfuscated",
    "register_extractor",
    "require_scope_api_version",
    "sweep_tokens",
]
