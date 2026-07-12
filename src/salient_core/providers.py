from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import metadata
from typing import Protocol, runtime_checkable

from .runtime import AgentBackend, JsonValue, ToolBundle

_PROVIDER_ENTRY_POINT_GROUP = "salient.agent_providers"
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_EMPTY_TOOL_BUNDLE = ToolBundle()


class ProviderName(str):
    def __new__(cls, value: str) -> ProviderName:
        if not _PROVIDER_NAME.fullmatch(value):
            raise ValueError(f"invalid provider name: {value!r}")
        return str.__new__(cls, value)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    streaming: bool
    tools: bool
    interruption: bool
    context_usage: bool


@dataclass(frozen=True, slots=True)
class ProviderProbe:
    available: bool
    detail: str


@runtime_checkable
class AgentProvider(Protocol):
    name: ProviderName
    capabilities: ProviderCapabilities

    async def probe(self) -> ProviderProbe: ...
    def create_backend(
        self,
        config: Mapping[str, JsonValue],
        *,
        tool_bundle: ToolBundle = _EMPTY_TOOL_BUNDLE,
    ) -> AgentBackend: ...


class DuplicateProviderError(LookupError):
    def __init__(self, name: ProviderName) -> None:
        self.name = name
        super().__init__(f"provider {name!r} is already registered")


class UnknownProviderError(LookupError):
    def __init__(self, name: ProviderName) -> None:
        self.name = name
        super().__init__(f"unknown provider {name!r}")


class InvalidProviderEntryPointError(TypeError):
    def __init__(self, entry_point: str) -> None:
        self.entry_point = entry_point
        super().__init__(f"entry point {entry_point!r} did not load an AgentProvider")


class ProviderRegistry:
    def __init__(self, providers: Iterable[AgentProvider] = ()) -> None:
        self._providers: dict[ProviderName, AgentProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: AgentProvider) -> None:
        name = ProviderName(provider.name)
        if name in self._providers:
            raise DuplicateProviderError(name)
        self._providers[name] = provider

    def get(self, name: ProviderName) -> AgentProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise UnknownProviderError(name) from None

    def providers(self) -> tuple[AgentProvider, ...]:
        return tuple(self._providers[name] for name in sorted(self._providers))

    def load_entry_points(self) -> None:
        entry_points = metadata.entry_points(group=_PROVIDER_ENTRY_POINT_GROUP)
        for entry_point in sorted(entry_points, key=lambda item: (item.name, item.value)):
            loaded = entry_point.load()
            provider = loaded() if isinstance(loaded, type) else loaded
            if not isinstance(provider, AgentProvider):
                raise InvalidProviderEntryPointError(entry_point.name)
            self.register(provider)


def builtin_provider_registry() -> ProviderRegistry:
    from .codex import CodexProvider
    from .daemon._backend import ClaudeProvider

    registry = ProviderRegistry((ClaudeProvider(), CodexProvider()))
    registry.load_entry_points()
    return registry


_active_provider_registry: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    global _active_provider_registry
    if _active_provider_registry is None:
        _active_provider_registry = builtin_provider_registry()
    return _active_provider_registry


def set_provider_registry(registry: ProviderRegistry) -> None:
    global _active_provider_registry
    _active_provider_registry = registry


def reset_provider_registry() -> None:
    global _active_provider_registry
    _active_provider_registry = None
