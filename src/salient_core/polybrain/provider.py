"""PolybrainProvider — AgentProvider for the multi-sub-brain API runtime.

Registered as a built-in next to Claude + Codex. Probe is env-key presence
only (key-file fallbacks from polybrain TS are a documented deferral).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ..providers import ProviderCapabilities, ProviderName, ProviderProbe
from ..runtime import JsonValue, ToolBundle
from .backend import DEFAULT_MAX_TURNS, PolybrainBackend, PolybrainBackendConfig, SafeguardHook
from .factory import BRAIN_SPECS, create_brain, get_spec

__all__ = ["PolybrainProvider"]

_EMPTY_TOOL_BUNDLE = ToolBundle()
DEFAULT_BRAIN = "minimax"
DEFAULT_MAX_TOKENS = 8192


class PolybrainProvider:
    name = ProviderName("polybrain")
    # Non-streaming v1 is honest (codex also coalesces); tools/interruption/
    # context-usage are real.
    capabilities = ProviderCapabilities(
        streaming=False, tools=True, interruption=True, context_usage=True
    )

    def __init__(self, *, brain_factory: Any = create_brain) -> None:
        self._brain_factory = brain_factory

    async def probe(self) -> ProviderProbe:
        ready = [
            spec.name
            for spec in BRAIN_SPECS.values()
            if any(os.environ.get(env) for env in spec.api_key_envs)
        ]
        if ready:
            return ProviderProbe(True, "sub-brain keys present: " + ", ".join(sorted(ready)))
        envs = sorted({env for spec in BRAIN_SPECS.values() for env in spec.api_key_envs})
        return ProviderProbe(False, "set one of: " + " / ".join(envs))

    def create_backend(
        self,
        config: Mapping[str, JsonValue],
        *,
        tool_bundle: ToolBundle = _EMPTY_TOOL_BUNDLE,
        safeguard_hook: SafeguardHook | None = None,
    ) -> PolybrainBackend:
        brain_name = config.get("brain", DEFAULT_BRAIN)
        if not isinstance(brain_name, str):
            raise TypeError("polybrain runtime brain must be a string")
        spec = get_spec(brain_name)

        model = config.get("model", spec.default_model)
        if not isinstance(model, str):
            raise TypeError("polybrain runtime model must be a string")
        base_url = config.get("base_url")
        if base_url is not None and not isinstance(base_url, str):
            raise TypeError("polybrain runtime base_url must be a string")
        api_key = config.get("api_key")
        if api_key is not None and not isinstance(api_key, str):
            raise TypeError("polybrain runtime api_key must be a string")
        api_key_env = config.get("api_key_env")
        if api_key_env is not None and not isinstance(api_key_env, str):
            raise TypeError("polybrain runtime api_key_env must be a string")
        instructions = config.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise TypeError("polybrain runtime instructions must be a string")
        agent_name = config.get("agent_name", "polybrain")
        if not isinstance(agent_name, str):
            raise TypeError("polybrain runtime agent_name must be a string")
        max_tokens = int(config.get("max_tokens", DEFAULT_MAX_TOKENS))  # type: ignore[arg-type]
        max_turns = int(config.get("max_turns", DEFAULT_MAX_TURNS))  # type: ignore[arg-type]
        temperature_raw = config.get("temperature")
        temperature = float(temperature_raw) if temperature_raw is not None else None  # type: ignore[arg-type]

        brain = self._brain_factory(
            brain_name,
            api_key=api_key,
            api_key_env=api_key_env,
            base_url=base_url,
            model=model,
        )
        return PolybrainBackend(
            PolybrainBackendConfig(
                brain=brain_name,
                model=model,
                instructions=instructions,
                agent_name=agent_name,
                max_tokens=max_tokens,
                temperature=temperature,
                max_turns=max_turns,
            ),
            brain=brain,
            spec=spec,
            tool_bundle=tool_bundle,
            safeguard_hook=safeguard_hook,
        )
