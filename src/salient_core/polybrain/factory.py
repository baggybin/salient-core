"""Sub-brain registry + factory.

Port of `../polybrain` `src/providers/factory.ts` for the OpenAI-compatible
API sub-brains (minimax / deepseek / glm). The TS CLI thin-shell providers
(agy / claude / codex / grok) are intentionally NOT ported — salient already
has first-class claude and codex paths.

API keys resolve env-only in v1 (explicit argument → `api_key_env` override →
the spec's env list). Polybrain TS also falls back to key files
(`~/.mmx/config.json`, `~/.deepseek/*`, `~/.zhipu/*`); that is a documented,
deliberate divergence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import MODEL_REGISTRY, PROVIDER_DEFAULT_MODEL
from .types import ModelInfo

if TYPE_CHECKING:
    import httpx

    from .openai_compat import OpenAICompatBrain

__all__ = [
    "BRAIN_SPECS",
    "BrainSpec",
    "MissingApiKeyError",
    "UnknownBrainError",
    "create_brain",
    "resolve_api_key",
]


@dataclass(frozen=True, slots=True)
class BrainSpec:
    name: str
    base_url: str
    api_key_envs: tuple[str, ...]
    default_model: str
    models: tuple[ModelInfo, ...]


BRAIN_SPECS: dict[str, BrainSpec] = {
    "minimax": BrainSpec(
        name="minimax",
        base_url="https://api.minimax.io/v1",
        api_key_envs=("MINIMAX_API_KEY",),
        default_model=PROVIDER_DEFAULT_MODEL["minimax"],
        models=MODEL_REGISTRY["minimax"],
    ),
    "deepseek": BrainSpec(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key_envs=("DEEPSEEK_API_KEY",),
        default_model=PROVIDER_DEFAULT_MODEL["deepseek"],
        models=MODEL_REGISTRY["deepseek"],
    ),
    "glm": BrainSpec(
        name="glm",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_envs=("GLM_API_KEY", "ZHIPU_API_KEY"),
        default_model=PROVIDER_DEFAULT_MODEL["glm"],
        models=MODEL_REGISTRY["glm"],
    ),
}


class UnknownBrainError(LookupError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"unknown polybrain sub-brain {name!r} (known: {', '.join(sorted(BRAIN_SPECS))})"
        )


class MissingApiKeyError(RuntimeError):
    def __init__(self, spec: BrainSpec) -> None:
        self.spec = spec
        envs = " / ".join(spec.api_key_envs)
        super().__init__(f"polybrain sub-brain {spec.name!r}: set {envs}")


def get_spec(name: str) -> BrainSpec:
    try:
        return BRAIN_SPECS[name]
    except KeyError:
        raise UnknownBrainError(name) from None


def resolve_api_key(
    spec: BrainSpec,
    *,
    api_key: str | None = None,
    api_key_env: str | None = None,
) -> str:
    if api_key:
        return api_key
    if api_key_env:
        value = os.environ.get(api_key_env)
        if value:
            return value
        raise MissingApiKeyError(
            BrainSpec(
                name=spec.name,
                base_url=spec.base_url,
                api_key_envs=(api_key_env,),
                default_model=spec.default_model,
                models=spec.models,
            )
        )
    for env in spec.api_key_envs:
        value = os.environ.get(env)
        if value:
            return value
    raise MissingApiKeyError(spec)


def create_brain(
    name: str,
    *,
    api_key: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> OpenAICompatBrain:
    """Build a chat-level brain for one sub-brain. The backend owns the loop."""
    from .openai_compat import OpenAICompatBrain

    spec = get_spec(name)
    key = resolve_api_key(spec, api_key=api_key, api_key_env=api_key_env)
    return OpenAICompatBrain(
        spec,
        api_key=key,
        base_url=base_url or spec.base_url,
        model=model or spec.default_model,
        client=client,
    )
