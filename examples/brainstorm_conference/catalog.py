"""Model catalog — the data layer behind any picker.

Enumerates the models a seat can be assigned, per provider family, and whether
that family's key is configured right now. deepseek/glm/minimax model lists come
straight from the kernel's ``MODEL_REGISTRY``; Claude and the endpoint providers
(openrouter/atlas) carry a small curated list here (their live catalogs are
available through ask-fable's ``list_openrouter_models`` / ``list_atlas_models``).

Pure and offline — a terminal picker, a web dialog, or a Claude Code selection
prompt can all render ``catalog()`` without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass

from panel import _CLAUDE_BRAINS, _resolve_anthropic, _resolve_key, _spec_for

from salient_core.polybrain.factory import BRAIN_SPECS
from salient_core.polybrain.types import ModelInfo

# Provider families a seat may be drawn from, in a sensible display order.
FAMILIES: tuple[str, ...] = ("deepseek", "glm", "minimax", "openrouter", "atlas", "claude")

_CLAUDE_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo("claude-opus-4-8", "Claude Opus 4.8", "Most capable", context_window=1_000_000),
    ModelInfo("claude-fable-5-1", "Claude Fable 5.1", "Long-horizon reasoning", context_window=1_000_000),
    ModelInfo("claude-sonnet-5", "Claude Sonnet 5", "Balanced", context_window=1_000_000),
)

# Curated shortlist for providers with no static kernel registry. The live, full
# catalogs live behind ask-fable's list_openrouter_models / list_atlas_models.
_CURATED: dict[str, tuple[ModelInfo, ...]] = {
    "openrouter": (
        ModelInfo("openai/gpt-4o-mini", "GPT-4o mini (OpenRouter)"),
        ModelInfo("anthropic/claude-3.5-sonnet", "Claude 3.5 Sonnet (OpenRouter)"),
        ModelInfo("deepseek/deepseek-chat", "DeepSeek Chat (OpenRouter)"),
        ModelInfo("google/gemini-flash-1.5", "Gemini Flash 1.5 (OpenRouter)"),
    ),
    "atlas": (
        ModelInfo("deepseek-ai/deepseek-v3", "DeepSeek V3 (Atlas)"),
        ModelInfo("zai-org/glm-5.2", "GLM 5.2 (Atlas)"),
        ModelInfo("openai/gpt-oss-120b", "GPT-OSS 120B (Atlas)"),
    ),
}


@dataclass(frozen=True, slots=True)
class FamilyCatalog:
    """One provider family: whether its key is set, and its selectable models."""

    brain: str
    available: bool
    models: tuple[ModelInfo, ...]


def models_for(brain: str) -> tuple[ModelInfo, ...]:
    """Selectable models for a provider family."""
    if brain in BRAIN_SPECS:
        return BRAIN_SPECS[brain].models
    if brain in _CLAUDE_BRAINS:
        return _CLAUDE_MODELS
    return _CURATED.get(brain, ())


def brain_available(brain: str) -> bool:
    """True when the family's API key/credentials are configured."""
    if brain in _CLAUDE_BRAINS:
        return _resolve_anthropic() is not None
    return _resolve_key(_spec_for(brain)) is not None


def catalog() -> list[FamilyCatalog]:
    """The full picker catalog: every family, availability, and its models."""
    return [
        FamilyCatalog(brain=b, available=brain_available(b), models=models_for(b))
        for b in FAMILIES
    ]
