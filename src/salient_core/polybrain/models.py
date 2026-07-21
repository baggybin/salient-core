"""Static model registries + defaults for polybrain sub-brains.

Port of `../polybrain` `src/providers/models.ts` static layers. Context-window
numbers are the static per-model values polybrain uses for its status gauge;
they also drive `get_context_usage` here. Live `/v1/models` listing with cache
and fuzzy matching is deliberately deferred (static registry v1).
"""

from __future__ import annotations

from .types import ModelInfo

__all__ = [
    "DEEPSEEK_MODELS",
    "GLM_MODELS",
    "MINIMAX_MODELS",
    "MODEL_REGISTRY",
    "PROVIDER_DEFAULT_MODEL",
    "find_model",
]

MINIMAX_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        "MiniMax-M2.7",
        "MiniMax M2.7",
        "Balanced general model",
        context_window=128_000,
    ),
    ModelInfo(
        "MiniMax-M2.7-highspeed",
        "MiniMax M2.7 Highspeed",
        "Low-latency variant",
        context_window=128_000,
    ),
    ModelInfo(
        "MiniMax-M3",
        "MiniMax M3",
        "Latest model, supports vision",
        supports_vision=True,
        is_reasoning=True,
        context_window=200_000,
    ),
)

DEEPSEEK_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        "deepseek-chat",
        "DeepSeek Chat",
        "DeepSeek V3 chat",
        context_window=128_000,
    ),
    ModelInfo(
        "deepseek-reasoner",
        "DeepSeek Reasoner",
        "DeepSeek R1 reasoning",
        is_reasoning=True,
        context_window=128_000,
    ),
)

GLM_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo("glm-4-plus", "GLM-4 Plus", "Strongest GLM-4", context_window=128_000),
    ModelInfo("glm-4-air", "GLM-4 Air", "Balanced GLM-4", context_window=128_000),
    ModelInfo("glm-4-airx", "GLM-4 AirX", "Fast, small context", context_window=8_000),
    ModelInfo("glm-4-flash", "GLM-4 Flash", "Free/fast tier", context_window=128_000),
    ModelInfo("glm-4-flashx", "GLM-4 FlashX", "Flash variant"),
    ModelInfo(
        "glm-zero-preview",
        "GLM Zero Preview",
        "Reasoning preview",
        is_reasoning=True,
        context_window=16_000,
    ),
)

MODEL_REGISTRY: dict[str, tuple[ModelInfo, ...]] = {
    "minimax": MINIMAX_MODELS,
    "deepseek": DEEPSEEK_MODELS,
    "glm": GLM_MODELS,
}

# Mirrors polybrain's PROVIDER_DEFAULT_MODEL for the API sub-brains. Note the
# TS REPL default is MiniMax-M2.7 while its ORACLE default is MiniMax-M3; we
# follow the oracle default (reasoning + vision) for agent duty.
PROVIDER_DEFAULT_MODEL: dict[str, str] = {
    "minimax": "MiniMax-M3",
    "deepseek": "deepseek-chat",
    "glm": "glm-4-flash",
}


def find_model(brain: str, model_id: str) -> ModelInfo | None:
    """Case-insensitive exact lookup in the static registry."""
    wanted = model_id.lower()
    for info in MODEL_REGISTRY.get(brain, ()):
        if info.id.lower() == wanted:
            return info
    return None
