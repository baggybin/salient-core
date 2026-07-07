"""Stable public facade for downstream scope extractors (SCOPE_API_VERSION).

A skin's registered extractor kinds (see `scope.register_extractor`) import
these names instead of reaching into the kernel's `_`-prefixed internals, so the
kernel can refactor those internals freely as long as this surface holds. Bump
`SCOPE_API_VERSION` when this facade changes in a way a skin must adapt to; a
skin asserts compatibility at startup.
"""

from __future__ import annotations

from .scope import (
    SCOPE_API_VERSION,
    ExtractorCtx,
    ExtractorError,
    ExtractorSpec,
    Target,
    register_extractor,
)

# Generic extraction primitives a domain extractor composes over.
from .scope import _classify_token as classify_token
from .scope import _is_obfuscated as is_obfuscated
from .scope import _sweep_tokens as sweep_tokens

__all__ = [
    "SCOPE_API_VERSION",
    "ExtractorCtx",
    "ExtractorError",
    "ExtractorSpec",
    "Target",
    "classify_token",
    "is_obfuscated",
    "register_extractor",
    "sweep_tokens",
]
