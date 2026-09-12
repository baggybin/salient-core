"""The debater bench — panelists that speak via the kernel's OpenAI-compatible
model client.

Reuses salient-core's ``PolybrainProvider`` machinery directly rather than
reinventing a client: ``deepseek`` / ``glm`` / ``minimax`` come straight from the
kernel's built-in ``BRAIN_SPECS``, and ``openrouter`` / ``atlas`` are the *same*
``OpenAICompatBrain`` pointed at their endpoints. Each panelist is one model
wearing a persona; the orchestrator hands it the running forum thread and asks
for its next contribution.

Two deliberate choices:

* **Distinct voices.** Each seat carries a ``persona`` fragment and we sample at
  a high temperature — the whole point of a conference is divergence, so we lean
  *against* the models collapsing into one polite average.
* **Claude seats are deferred.** ``fable`` / ``opus`` run on an OAuth
  subscription, not a plain API key, so they aren't wired here in v1. The wider
  non-Claude bench works from API keys alone; a Claude seat can be added later
  via ``ANTHROPIC_API_KEY`` or the daemon's Claude provider.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from anthropic_compat import AnthropicBrain

from salient_core.polybrain.factory import BRAIN_SPECS, BrainSpec
from salient_core.polybrain.openai_compat import OpenAICompatBrain
from salient_core.polybrain.types import ChatMessage


def _generic_spec(
    name: str, default_base_url: str, key_envs: tuple[str, ...], default_model: str
) -> BrainSpec:
    """A spec for an OpenAI-compatible provider the kernel doesn't ship. The
    base URL is env-overridable (``<NAME>_BASE_URL``) so a deployment can point
    at a proxy or region without touching code."""
    base = os.environ.get(f"{name.upper()}_BASE_URL", default_base_url)
    return BrainSpec(
        name=name,
        base_url=base,
        api_key_envs=key_envs,
        default_model=default_model,
        models=(),
    )


# OpenAI-compatible providers with no built-in kernel spec. openrouter's base URL
# is stable; atlas's is env-overridable (ATLAS_BASE_URL) since deployments differ.
_EXTRA_SPECS: dict[str, BrainSpec] = {
    "openrouter": _generic_spec(
        "openrouter",
        "https://openrouter.ai/api/v1",
        ("OPENROUTER_API_KEY",),
        "openai/gpt-4o-mini",
    ),
    # base URL confirmed against ask-fable's src/ask_fable/atlas.py (chat lands at
    # /v1/chat/completions); ASK_FABLE_ATLAS_BASE_URL mirrors ask-fable's override.
    "atlas": _generic_spec(
        "atlas",
        os.environ.get("ASK_FABLE_ATLAS_BASE_URL", "https://api.atlascloud.ai/v1"),
        ("ATLASCLOUD_API_KEY", "ATLAS_API_KEY"),
        "deepseek-ai/deepseek-v3",
    ),
}

# Claude seats speak Anthropic's Messages API (a different wire shape) so they
# route to AnthropicBrain, not the OpenAI-compatible client. fable/opus run on an
# OAuth subscription — set ANTHROPIC_AUTH_TOKEN (+ ANTHROPIC_BASE_URL) for a proxy,
# or ANTHROPIC_API_KEY for a direct key.
_CLAUDE_BRAINS: dict[str, str] = {
    "claude": "claude-sonnet-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5-1",
}


def known_brains() -> list[str]:
    """Every brain a seat may name."""
    return sorted({*BRAIN_SPECS, *_EXTRA_SPECS, *_CLAUDE_BRAINS})


@dataclass(frozen=True, slots=True)
class Seat:
    """One chair at the table: a display label, which brain/provider, the model
    id (``None`` ⇒ the brain's default), and a persona fragment that diversifies
    the voice."""

    label: str
    brain: str
    model: str | None = None
    persona: str = ""


class MissingSeatKeyError(RuntimeError):
    """Raised when no API key is set for a seat's brain — the orchestrator
    catches this to skip the seat and report it, rather than crashing the run."""

    def __init__(self, seat: Seat, spec: BrainSpec) -> None:
        self.seat = seat
        self.spec = spec
        envs = " / ".join(spec.api_key_envs)
        super().__init__(f"seat {seat.label!r} ({spec.name}) needs one of: {envs}")


def _spec_for(brain: str) -> BrainSpec:
    if brain in BRAIN_SPECS:
        return BRAIN_SPECS[brain]
    if brain in _EXTRA_SPECS:
        return _EXTRA_SPECS[brain]
    raise ValueError(f"unknown brain {brain!r}; known: {known_brains()}")


def _resolve_key(spec: BrainSpec) -> str | None:
    for env in spec.api_key_envs:
        value = os.environ.get(env)
        if value:
            return value
    return None


class Panelist:
    """One debater: a model + persona that reads the thread and adds a turn."""

    def __init__(self, seat: Seat, brain: OpenAICompatBrain | AnthropicBrain) -> None:
        self.seat = seat
        self.label = seat.label
        self._brain = brain

    async def speak(
        self, *, system: str, thread: str, temperature: float | None = 0.9
    ) -> str:
        """Read the running thread and produce this panelist's next contribution.

        High default temperature is intentional: divergence is the goal, so we
        make the bench argue, not converge to the mean.
        """
        persona = f"\n\n{self.seat.persona}" if self.seat.persona else ""
        messages = [
            ChatMessage(role="system", content=system + persona),
            ChatMessage(role="user", content=thread),
        ]
        reply = await self._brain.chat(messages=messages, temperature=temperature)
        return (reply.text or "").strip()

    async def aclose(self) -> None:
        await self._brain.aclose()


def _claude_pseudo_spec(brain: str) -> BrainSpec:
    """A spec carrying just the Claude key envs, for a clear MissingSeatKeyError."""
    return BrainSpec(
        name=brain,
        base_url="https://api.anthropic.com",
        api_key_envs=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        default_model=_CLAUDE_BRAINS[brain],
        models=(),
    )


def _resolve_anthropic() -> tuple[str, str] | None:
    """(key, auth_style) for a Claude seat, or None. A bearer AUTH_TOKEN (the
    OAuth/proxy path) wins over a direct API key."""
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if token:
        return token, "bearer"
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key, "api_key"
    return None


def _build_claude_panelist(
    seat: Seat, *, client: httpx.AsyncClient | None = None
) -> Panelist:
    creds = _resolve_anthropic()
    if creds is None and client is None:
        raise MissingSeatKeyError(seat, _claude_pseudo_spec(seat.brain))
    key, auth_style = creds or ("test-key", "api_key")
    brain = AnthropicBrain(
        model=seat.model or _CLAUDE_BRAINS[seat.brain],
        api_key=key,
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        auth_style=auth_style,
        client=client,
    )
    return Panelist(seat, brain)


def build_panelist(seat: Seat, *, client: httpx.AsyncClient | None = None) -> Panelist:
    """Build a panelist for ``seat``. Raises :class:`MissingSeatKeyError` when no
    key is set (unless an ``httpx`` client is injected, as tests do)."""
    if seat.brain in _CLAUDE_BRAINS:
        return _build_claude_panelist(seat, client=client)
    spec = _spec_for(seat.brain)
    key = _resolve_key(spec)
    if key is None and client is None:
        raise MissingSeatKeyError(seat, spec)
    brain = OpenAICompatBrain(
        spec,
        api_key=key or "test-key",
        base_url=spec.base_url,
        model=seat.model or spec.default_model,
        client=client,
    )
    return Panelist(seat, brain)


# A sensible default bench: three distinct non-Claude reasoners with clashing
# dispositions, to seed genuine disagreement. openrouter / atlas seats can be
# added by the caller once their keys/endpoints are set.
DEFAULT_ROSTER: tuple[Seat, ...] = (
    Seat("deepseek", "deepseek", persona="You argue from rigorous, systems-level first principles."),
    Seat("glm", "glm", persona="You attack assumptions and hunt the unconventional angle."),
    Seat("minimax", "minimax", persona="You push the boldest, most ambitious version of any idea."),
)
