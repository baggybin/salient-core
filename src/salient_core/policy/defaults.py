"""The kernel's generic default policy dataset.

Built from the ``_DEFAULT_*`` tables that live in ``scope`` / ``safeguards``.
The kernel's own gate and tests run against this until a downstream skin
registers its own via ``registry.set_active``. Wrapped in read-only proxies
so the default can't be mutated in place.
"""

from __future__ import annotations

from types import MappingProxyType

from .registry import PolicyDataset
from .safeguards import (
    _DEFAULT_LOUD_PATTERNS,
    _DEFAULT_PROHIBITED_PATTERNS,
    _NATURAL_LANGUAGE_PROHIBITED,
)
from .scope import _DEFAULT_TOOL_TARGETS

DEFAULT_DATASET = PolicyDataset(
    tool_targets=MappingProxyType(dict(_DEFAULT_TOOL_TARGETS)),
    prohibited_patterns=MappingProxyType(dict(_DEFAULT_PROHIBITED_PATTERNS)),
    loud_patterns=MappingProxyType(dict(_DEFAULT_LOUD_PATTERNS)),
    natural_language_prohibited=tuple(_NATURAL_LANGUAGE_PROHIBITED),
)
