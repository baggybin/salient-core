"""salient-core — a multi-agent coordination kernel.

Bus-as-MCP-server, deterministic policy gates (scope + safeguards),
noisy-OR knowledge graph, operator-mediated delegation, and an SM-2
spaced-repetition scheduler. Claude-SDK-specific for v1, with the seam
drawn for multi-SDK v2.

The names re-exported here are the supported public surface — import them
from ``salient_core`` (e.g. ``from salient_core import KnowledgeGraph``)
rather than reaching into the private ``salient_core.<subpkg>._*`` modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.4.0"

# The supported public surface, resolved lazily (PEP 562) so that
# `import salient_core` — and any submodule import, which runs this file
# first — doesn't eagerly load the heavy daemon runtime (.bus/.daemon pull
# in claude_agent_sdk, ~0.3s). Pure-data consumers (KnowledgeGraph, the SM-2
# scheduler) pay only for what they touch. The TYPE_CHECKING block mirrors
# the map so mypy/py.typed consumers resolve the same names statically.
if TYPE_CHECKING:
    from .bus import ContextStore as ContextStore
    from .bus import make_bus as make_bus
    from .coord import Question as Question
    from .coord import QuestionInbox as QuestionInbox
    from .daemon import AgentRunner as AgentRunner
    from .daemon import EventHub as EventHub
    from .daemon import Job as Job
    from .daemon import spawn_background as spawn_background
    from .memory import Action as Action
    from .memory import ActionLedger as ActionLedger
    from .memory import Embedder as Embedder
    from .memory import EmbeddingConfig as EmbeddingConfig
    from .memory import Fact as Fact
    from .memory import KnowledgeGraph as KnowledgeGraph
    from .memory import get_embedder as get_embedder
    from .memory import semantic_recall as semantic_recall
    from .protocols import AgentBackend as AgentBackend
    from .protocols import AliasProtocol as AliasProtocol
    from .protocols import DaemonServices as DaemonServices
    from .protocols import ToolBuilder as ToolBuilder
    from .tutor import bucketed_profile as bucketed_profile

_LAZY_EXPORTS = {
    "ContextStore": ".bus",
    "make_bus": ".bus",
    "Question": ".coord",
    "QuestionInbox": ".coord",
    "AgentRunner": ".daemon",
    "EventHub": ".daemon",
    "Job": ".daemon",
    "spawn_background": ".daemon",
    "Action": ".memory",
    "ActionLedger": ".memory",
    "Embedder": ".memory",
    "EmbeddingConfig": ".memory",
    "Fact": ".memory",
    "KnowledgeGraph": ".memory",
    "get_embedder": ".memory",
    "semantic_recall": ".memory",
    "AgentBackend": ".protocols",
    "AliasProtocol": ".protocols",
    "DaemonServices": ".protocols",
    "ToolBuilder": ".protocols",
    "bucketed_profile": ".tutor",
}

_SUBPACKAGES = frozenset(
    {"alias", "bus", "coord", "daemon", "display", "memory", "policy", "protocols", "tutor"}
)

# Derived, not hand-listed: _LAZY_EXPORTS is the single source of truth for
# the public surface (tests assert the TYPE_CHECKING block mirrors it).
__all__ = sorted([*_LAZY_EXPORTS, "__version__"])


def __getattr__(name: str) -> Any:
    from importlib import import_module

    submodule = _LAZY_EXPORTS.get(name)
    if submodule is not None:
        value = getattr(import_module(submodule, __name__), name)
        globals()[name] = value  # cache: next access skips __getattr__
        return value
    if name in _SUBPACKAGES:
        return import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS) | _SUBPACKAGES)
