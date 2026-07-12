"""Policy data seam — inject domain-specific scope/safeguard data.

The kernel ships a GENERIC default dataset (``defaults.DEFAULT_DATASET``) so
its own gate logic and tests have data to run against. A downstream skin (a
security app, the tutor, …) registers its own dataset at startup via
``set_active`` and the kernel's gate consults it thereafter — the same idiom
as ``salient_core.alias.set_active``.

The three tables the policy gate consults:
    tool_targets        — {wire-name → ExtractorSpec}: how to pull a scope
                          "target" out of each tool's args (scope.gate,
                          actions.target_key_for_call, the runner gate).
    prohibited_patterns — {qualified-tool → [(label, regex)]}: deny patterns
                          (safeguards.check_intent).
    loud_patterns       — {qualified-tool → [(label, regex)]}: flag-but-allow
                          patterns (safeguards.check_posture).

Consumers read the ACTIVE dataset. Pure functions accept an explicit
``dataset=`` for test isolation (no global state) and fall back to
``get_active()`` in production, where one-process-one-policy holds. Compile
any derived state (e.g. regexes) into the dataset up front so a warm cache
can't outlive a ``set_active`` — the dataset is the single source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scope import ExtractorSpec

# (label, regex) — a labelled deny/flag pattern.
Pattern = tuple[str, str]


def _freeze_patterns(m: Mapping[str, Sequence[Pattern]]) -> Mapping[str, Sequence[Pattern]]:
    """Canonicalize a {tool → [(label, regex), ...]} mapping into a read-only
    mapping of tuples so external code holding the original list/dict can't
    mutate registered policy after the fact."""
    return MappingProxyType({k: tuple(v) for k, v in m.items()})


@dataclass(frozen=True)
class PolicyDataset:
    """Immutable bundle of the data the policy gate consults. Frozen so live
    security policy can't be mutated after registration; swap the whole
    dataset via ``set_active`` instead.

    ``frozen=True`` only blocks field REASSIGNMENT; the mutable mapping/sequence
    inputs are canonicalized into read-only structures in ``__post_init__`` so
    DEEP mutation through a retained reference to the original input can't reach
    live policy either."""

    tool_targets: Mapping[str, ExtractorSpec]
    prohibited_patterns: Mapping[str, Sequence[Pattern]]
    loud_patterns: Mapping[str, Sequence[Pattern]]
    # Natural-language prohibited-intent markers scanned by
    # ``safeguards.check_prompt_intent`` (operator / delegation prompts).
    natural_language_prohibited: Sequence[Pattern] = ()
    # Qualified tool names whose recursive transfer of a whole system tree is a
    # structural prohibited shape (see ``safeguards._structural_block``). Empty
    # by default; a downstream dataset lists its own file-transfer tools.
    structural_transfer_tools: frozenset[str] = frozenset()
    # Deprecated compatibility input for shadow-mode migration only. It never
    # authorizes enforce mode; use qualified ``tool_targets`` classifications.
    trusted_builtins: frozenset[str] = field(
        default_factory=frozenset,
        metadata={
            "deprecated": (
                "trusted_builtins is shadow-only compatibility; migrate each "
                "tool to a qualified PolicyDataset.tool_targets entry"
            )
        },
    )

    def __post_init__(self) -> None:
        # Canonicalize into read-only structures. object.__setattr__ is the
        # frozen-dataclass idiom for writing a field inside __post_init__.
        object.__setattr__(self, "tool_targets", MappingProxyType(dict(self.tool_targets)))
        object.__setattr__(self, "prohibited_patterns", _freeze_patterns(self.prohibited_patterns))
        object.__setattr__(self, "loud_patterns", _freeze_patterns(self.loud_patterns))
        object.__setattr__(
            self, "natural_language_prohibited", tuple(self.natural_language_prohibited)
        )
        object.__setattr__(
            self, "structural_transfer_tools", frozenset(self.structural_transfer_tools)
        )
        object.__setattr__(self, "trusted_builtins", frozenset(self.trusted_builtins))


_active: PolicyDataset | None = None


def set_active(dataset: PolicyDataset) -> None:
    """Register the active policy dataset. A downstream skin calls this once
    at startup (next to ``alias.set_active``). Replaces any prior dataset."""
    global _active
    _active = dataset


def get_active() -> PolicyDataset:
    """The active dataset, defaulting to the kernel's generic bundle until a
    downstream registers its own. Lazy import of ``defaults`` breaks the
    registry↔scope/safeguards import cycle."""
    global _active
    if _active is None:
        from .defaults import DEFAULT_DATASET

        _active = DEFAULT_DATASET
    return _active


def reset() -> None:
    """Restore the default dataset. Test-only — call between tests (or via an
    autouse fixture) so a ``set_active`` in one test can't leak into another."""
    global _active
    _active = None
