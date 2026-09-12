"""Shared protocols and value types for the conference roles.

Kept dependency-free so ``moderator`` / ``rapporteur`` / ``conference`` can share
these without import cycles. ``Speaker`` is structural — anything that can
``speak`` (a :class:`panel.Panelist`, or a test stub) satisfies it.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Speaker(Protocol):
    """A model that reads the thread and produces its next contribution."""

    label: str

    def speak(
        self, *, system: str, thread: str, temperature: float | None = None
    ) -> Awaitable[str]: ...


class Chair(Protocol):
    """The moderator surface the orchestrator drives: pick who speaks next."""

    async def next_turn(
        self, *, thread: str, round: int, last_speaker: str | None
    ) -> Turn: ...


@dataclass(frozen=True, slots=True)
class Turn:
    """The chair's decision for one turn: who speaks, and the directive it hands
    them (the provocation that keeps the room from agreeing too soon)."""

    speaker: str
    instruction: str


@dataclass(frozen=True, slots=True)
class IdeaCluster:
    """One idea and the distinct panelists who independently backed it — the
    'surprise detector' signal, ranked by cross-model support."""

    label: str
    supporters: tuple[str, ...]

    @property
    def support(self) -> int:
        return len(self.supporters)


@dataclass(frozen=True, slots=True)
class Report:
    """The rapporteur's deliverable: the map of the disagreement (prose) plus the
    ranked idea leaderboard (may be empty in v1)."""

    map_text: str
    ideas: tuple[IdeaCluster, ...] = ()
