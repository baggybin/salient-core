"""Polybrain — multi-vendor OpenAI-compatible API-brain runtime.

Sub-brains (minimax / deepseek / glm) share one chat-level client; the
`PolybrainBackend` owns the agent turn loop and executes tools exclusively
through the kernel `ToolBundle` (scope-gated handlers + a safeguard hook
injected by the runner factory). See
`salient/docs/plans/POLYBRAIN_MODULE_GROK_2026-07-19.md`.
"""

from .backend import PolybrainBackend, PolybrainBackendConfig, SafeguardHook
from .factory import (
    BRAIN_SPECS,
    BrainSpec,
    MissingApiKeyError,
    UnknownBrainError,
    create_brain,
)
from .models import MODEL_REGISTRY, PROVIDER_DEFAULT_MODEL, find_model
from .openai_compat import BrainError, OpenAICompatBrain
from .provider import PolybrainProvider
from .types import (
    AssistantReply,
    Brain,
    BrainToolCall,
    ChatMessage,
    ModelInfo,
    Usage,
)

__all__ = [
    "BRAIN_SPECS",
    "MODEL_REGISTRY",
    "PROVIDER_DEFAULT_MODEL",
    "AssistantReply",
    "Brain",
    "BrainError",
    "BrainSpec",
    "BrainToolCall",
    "ChatMessage",
    "MissingApiKeyError",
    "ModelInfo",
    "OpenAICompatBrain",
    "PolybrainBackend",
    "PolybrainBackendConfig",
    "PolybrainProvider",
    "SafeguardHook",
    "UnknownBrainError",
    "Usage",
    "create_brain",
    "find_model",
]
