"""The rapporteur — turns the argument into the deliverable.

A transcript is a lovely read and useless; the value is the *map of the
disagreement* plus the *ranked idea leaderboard*. This role reads the whole forum
once at the end: an LLM distils the map (converged / crux / what-would-change),
and the leaderboard clusters near-duplicate ideas and ranks them by cross-model
support (see ``leaderboard.py``).
"""

from __future__ import annotations

from forum import ForumStore
from leaderboard import build_leaderboard
from roles import Report, Speaker

RAPPORTEUR_SYSTEM = """You are the RAPPORTEUR for a brainstorming conference. Read \
the whole discussion and produce a MAP OF THE DISAGREEMENT — not a summary, not a \
tidy consensus. Structure it exactly as:

CONVERGED — the points the participants genuinely ended up agreeing on.
THE CRUX — the ONE disagreement they could not resolve, stated sharply, and WHY it \
held (what real trade-off or unknown sits under it).
WHAT WOULD CHANGE IT — the specific fact, constraint, or test that would settle the \
crux one way or the other.

Be concrete and name positions. If they never really disagreed, say so plainly — \
that is itself a finding (and usually a sign the room was too polite)."""


class Rapporteur:
    """Reads the final forum and emits the map of the disagreement + leaderboard."""

    def __init__(self, chair: Speaker, *, embedder: object | None = None) -> None:
        self._chair = chair
        self._embedder = embedder

    async def summarize(self, forum: ForumStore) -> Report:
        transcript = forum.transcript()
        map_text = await self._chair.speak(
            system=RAPPORTEUR_SYSTEM, thread=transcript, temperature=0.3
        )
        # The seed and any chair posts are book-keeping, not ideas to rank.
        posts = [(e.agent, e.text) for e in forum.read() if e.agent != "moderator"]
        ideas = await build_leaderboard(posts, self._embedder)
        return Report(map_text=(map_text or "").strip(), ideas=tuple(ideas))
