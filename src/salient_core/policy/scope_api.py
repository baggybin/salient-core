"""Stable public facade for downstream scope extractors (SCOPE_API_VERSION).

A skin's registered extractor kinds (see `scope.register_extractor`) import
these names instead of reaching into the kernel's `_`-prefixed internals, so the
kernel can refactor those internals freely as long as this surface holds. Bump
`SCOPE_API_VERSION` when this facade changes in a way a skin must adapt to; a
skin asserts compatibility at startup.

Two groups live here:

- **Extraction** — what a domain extractor composes over (`Target`,
  `ExtractorSpec`, `ExtractorCtx`, `classify_token`, …).
- **Evaluation** — the entry point a skin needs when a tool must re-judge a
  destination the gate never saw. `gate()` runs once, against the call's
  ARGUMENTS. A tool that can be redirected mid-call (an HTTP `Location`, a
  follow-up dial) has to judge the NEW destination against the same rules
  and the same lane, and the only correct way to do that is to call the
  kernel's own `evaluate_scope` — reimplementing lane selection skin-side
  makes a second, weaker copy of the rules, which is how a floor quietly
  becomes decoration.

`evaluate_scope` and the invocation types were promoted here after
`salient-assay` reached into `policy.decision` / `policy.scope_evaluation`
directly to build its redirect floor: it was depending on kernel internals
that this facade's contract did not cover.

The promotion is ADDITIVE, so `SCOPE_API_VERSION` is deliberately NOT bumped.
No existing skin must adapt, and `require_scope_api_version` is an
exact-equality check — bumping to announce "the facade got bigger" would
break every pinned skin for no functional reason. The honest cost: a skin
using these names against a kernel that predates them gets an ImportError
rather than a clean `ScopeApiVersionError`. Changing or REMOVING any name
below is still a bump.
"""

from __future__ import annotations

from dataclasses import dataclass

# Evaluation: re-judging a destination the gate never saw (see module docstring).
from .decision import InvocationIdentity, InvocationTransport, ToolInvocation
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
from .scope_evaluation import ScopeEvaluation, ScopeEvaluationKind, evaluate_scope


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
    "InvocationIdentity",
    "InvocationTransport",
    "PrincipalResourceRelationship",
    "PrincipalResourceRequirement",
    "ProviderTargetBinding",
    "ScopeEvaluation",
    "ScopeEvaluationKind",
    "Target",
    "TargetIdentity",
    "ToolInvocation",
    "classify_token",
    "evaluate_scope",
    "is_obfuscated",
    "register_extractor",
    "require_scope_api_version",
    "sweep_tokens",
]
