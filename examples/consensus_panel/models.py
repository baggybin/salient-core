"""Model catalog for the consensus-panel showcase — the picker's data source.

The panel UI lets an operator assign a different model to each leg, so it needs
a list of models to choose from. This module returns that list, preferring the
live Anthropic Models API (``client.models.list()``) when the ``anthropic`` SDK
and a credential are present, and falling back to a curated static catalog when
they aren't — so the demo runs offline (and in CI) without a key.

Pure and dependency-light: ``anthropic`` is imported lazily inside the live
path, so importing this module never requires the SDK.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("salient.examples.consensus_panel.models")


@dataclass(frozen=True)
class ModelChoice:
    """One entry in the model picker."""

    id: str
    display_name: str
    # Rough context window in tokens, when known (Models API `max_input_tokens`).
    context_tokens: int | None = None
    # True when sourced from the live Models API rather than the static fallback.
    live: bool = False


# Curated fallback — current Claude model IDs as of the showcase's cutoff. Kept
# small and current; the live path supersedes it whenever the SDK is configured.
_FALLBACK: tuple[ModelChoice, ...] = (
    ModelChoice("claude-fable-5", "Claude Fable 5", 1_000_000),
    ModelChoice("claude-opus-4-8", "Claude Opus 4.8", 1_000_000),
    ModelChoice("claude-sonnet-5", "Claude Sonnet 5", 1_000_000),
    ModelChoice("claude-haiku-4-5", "Claude Haiku 4.5", 200_000),
)


def fallback_catalog() -> list[ModelChoice]:
    """The static catalog used when the live Models API is unavailable."""
    return list(_FALLBACK)


# The catalog is effectively constant for the process lifetime, and the live
# path is a paginated network round-trip — fetch once, then serve the memo.
_CACHE: list[ModelChoice] | None = None


def list_models() -> list[ModelChoice]:
    """Return the model choices for the picker.

    Tries the live Anthropic Models API first; on any failure (SDK missing, no
    credential, network error) logs once and returns :func:`fallback_catalog`.
    Never raises — the picker always has something to show. The result is
    cached for the process lifetime (restart the server to pick up new models).
    """
    global _CACHE
    if _CACHE is not None:
        return list(_CACHE)
    _CACHE = _fetch_models()
    return list(_CACHE)


def _fetch_models() -> list[ModelChoice]:
    try:
        import anthropic
    except ImportError:
        _log.info("anthropic SDK not installed — using the static model catalog")
        return fallback_catalog()

    # Stable, human-friendly order: newest first, made deterministic by sorting
    # on (created_at desc, id); undated entries last. The sort is inside the try
    # so an unexpected created_at shape (non-datetime) degrades to the fallback
    # rather than breaking the documented never-raises contract.
    def _key(pair: tuple[Any, ModelChoice]) -> tuple[float, str]:
        ts, choice = pair
        return (-ts.timestamp() if ts is not None else float("inf"), choice.id)

    try:
        client = anthropic.Anthropic()
        # `models.list()` auto-paginates when iterated directly.
        dated = [
            (
                getattr(m, "created_at", None),
                ModelChoice(
                    id=m.id,
                    display_name=getattr(m, "display_name", m.id) or m.id,
                    context_tokens=getattr(m, "max_input_tokens", None),
                    live=True,
                ),
            )
            for m in client.models.list()
        ]
        ordered = [c for _, c in sorted(dated, key=_key)]
    except Exception:  # noqa: BLE001 — the picker must degrade, never crash
        _log.warning("Models API unavailable — using the static model catalog", exc_info=True)
        return fallback_catalog()

    return ordered or fallback_catalog()
