"""Tool-builder injection seam — a downstream registers its tool factories.

The kernel ships no tool factories; the runner-factory builds an agent's tool
MCP server by calling the *registered* builder. A downstream skin (a security
app, the tutor) calls ``set_tool_builder`` at startup — the same idiom as
``alias.set_active`` and ``policy.registry.set_active``. Until then the default
builder raises, so a missed registration fails loudly at first agent start
rather than silently producing a tool-less daemon.

See ``protocols.ToolBuilder`` for the builder's call contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..protocols import ToolBuildContext, ToolBundleBuilder
from ..runtime import JsonValue, ToolBundle


def _stub_build(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError(
        "tool builder not registered — call "
        "salient_core.daemon.set_tool_builder(build[, build_subagents]) at "
        "startup. A downstream skin provides the real tool factories."
    )


def _stub_build_subagents(*args: Any, **kwargs: Any) -> list:
    # Subagents are optional; absent a registration there simply are none.
    return []


_build: Callable[..., Any] = _stub_build
_build_subagents: Callable[..., list] = _stub_build_subagents


def _stub_build_bundle(
    tool_type: str,
    config: Mapping[str, JsonValue],
    *,
    context: ToolBuildContext,
) -> ToolBundle:
    del tool_type, config, context
    raise NotImplementedError(
        "tool bundle builder not registered — call "
        "salient_core.daemon.set_tool_bundle_builder(build) at startup"
    )


_build_bundle: ToolBundleBuilder = _stub_build_bundle


def set_tool_builder(
    build: Callable[..., Any],
    build_subagents: Callable[..., list] | None = None,
) -> None:
    """Register the tool builder (and optionally the subagent builder).
    Called once at startup by a downstream skin."""
    global _build, _build_subagents
    _build = build
    if build_subagents is not None:
        _build_subagents = build_subagents


def get_tool_builder() -> Callable[..., Any]:
    """The active tool builder (raising stub until a skin registers one)."""
    return _build


def set_tool_bundle_builder(build: ToolBundleBuilder) -> None:
    global _build_bundle
    _build_bundle = build


def get_tool_bundle_builder() -> ToolBundleBuilder:
    return _build_bundle


def get_subagent_builder() -> Callable[..., list]:
    """The active subagent builder (no-op returning [] until registered)."""
    return _build_subagents


# Tool-type → wire method name(s), e.g. {"bash": "run", "http": "get"}. A
# sibling of the tool builder: the prompt-assembly layer reads it to name an
# agent's PRIMARY action tool (`<name>.<wire>`) in its system prompt. Skin
# data — the kernel ships none, so the default is empty and the tools block
# simply omits the primary-tool line (graceful, never a crash) until a skin
# registers its map at startup, alongside set_tool_builder.
_wire_names: dict[str, str | list[str]] = {}


def set_tool_wire_names(mapping: dict[str, str | list[str]]) -> None:
    """Register the tool-type → wire-name map. Called once at startup by a
    downstream skin, next to ``set_tool_builder``."""
    global _wire_names
    _wire_names = mapping


def get_tool_wire_names() -> dict[str, str | list[str]]:
    """The active tool-type → wire-name map (empty until a skin registers)."""
    return _wire_names


# KG-builder seam — how a daemon's KnowledgeGraph gets constructed. Unlike the
# tool builder there is a perfectly good kernel default (the local SQLite
# store), so the unregistered state BUILDS rather than raises. A downstream
# registers an alternative at startup — e.g. a network client with the same
# method surface (e.g. a remote KnowledgeGraph client) — and every daemon
# that constructs its KG through get_kg_builder() picks it up, with zero
# changes to the bus tools that consume ``daemon.kg``.


def _default_build_kg(db_path: Any) -> Any:
    from ..memory.kg import KnowledgeGraph  # lazy: keep daemon import light

    return KnowledgeGraph(Path(db_path))


_build_kg: Callable[..., Any] = _default_build_kg


def set_kg_builder(build: Callable[..., Any]) -> None:
    """Register the KG builder — ``build(db_path)`` returns the object bound as
    ``daemon.kg`` (anything with the KnowledgeGraph method surface). Called once
    at startup by a downstream skin; the default builds the local SQLite store."""
    global _build_kg
    _build_kg = build


def get_kg_builder() -> Callable[..., Any]:
    """The active KG builder (the local-KnowledgeGraph default until a skin
    registers one)."""
    return _build_kg


# Daemon skin-module registry — the runner-factory reaches into a few downstream
# SKIN modules the kernel doesn't ship (engagement profile resolution, tool-action
# classification, plugin manifests). Same shape as the bus's set_bus_skin_modules:
# a downstream registers them by name (module or lazy thunk) at startup; the
# factory resolves at call time. `engagement` is special-cased with permissive
# kernel defaults (nothing disabled / empty profile block) so the kernel stays
# runnable standalone; the others raise a clear error if a tool path needs them
# without a skin registered.
_daemon_skin_modules: dict[str, Any] = {}


def set_daemon_skin_modules(**modules: Any) -> None:
    """Register downstream skin modules the runner-factory reaches into (by
    keyword — ``engagement=...``, ``action_class=...``, ``plugins=...``). A value
    may be the module or a zero-arg thunk returning it. None values ignored."""
    _daemon_skin_modules.update({k: v for k, v in modules.items() if v is not None})


def get_daemon_skin_module(name: str, *, required: bool = True) -> Any:
    """The registered skin module ``name`` (a thunk is resolved + cached on first
    use). With ``required=False`` returns None when unregistered instead of
    raising — used for engagement, which has permissive kernel defaults."""
    mod = _daemon_skin_modules.get(name)
    if mod is None:
        if required:
            raise RuntimeError(
                f"daemon skin module {name!r} is not registered — the downstream "
                f"must call salient_core.daemon.set_daemon_skin_modules({name}=...)"
            )
        return None
    if callable(mod):  # a thunk (a module is not callable) — resolve + cache
        mod = mod()
        _daemon_skin_modules[name] = mod
    return mod


def reset() -> None:
    """Restore the raising/no-op defaults. Test-only."""
    global _build, _build_bundle, _build_subagents, _build_kg, _wire_names
    _build, _build_subagents = _stub_build, _stub_build_subagents
    _build_bundle = _stub_build_bundle
    _build_kg = _default_build_kg
    _wire_names = {}
    _daemon_skin_modules.clear()
