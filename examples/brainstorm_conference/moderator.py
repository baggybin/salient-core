"""The chair — the judgement half of the hybrid moderator.

An LLM reads the running thread each turn and decides *who speaks next* and what
provocation to hand them. Its whole job is to fight the sycophantic collapse:
call on the quiet dissenter, forbid easy agreement, push the mad idea further.
(The *when-to-stop* decision is not here — that's the deterministic convergence
score in ``convergence.py``. That split is the "hybrid".)

Robustness: the chair is asked for a small JSON object, but LLMs wrap JSON in
prose, so parsing is lenient and any failure falls back to round-robin with an
adversarial directive — the conference never stalls on a malformed turn.
"""

from __future__ import annotations

import json
import re

from roles import Chair, Speaker, Turn

MODERATOR_SYSTEM = """You are the CHAIR of a brainstorming conference. Your job is \
NOT to summarise or to seek agreement — it is to keep a genuine argument alive and \
DIVERGENT. Read the discussion, then choose who should speak next and give them a \
sharp directive.

Prefer the participant who has gone quiet, or who most disagrees with the last \
point, or who could push a raw idea further. Never let the room settle into polite \
consensus — if they are converging too fast, provoke a counter-position.

Respond with ONLY a JSON object, no other text:
{{"speaker": "<one of: {roster}>", "instruction": "<a sharp directive that prevents \
easy agreement and demands something new>"}}"""

_JSON = re.compile(r"\{.*\}", re.S)

_DEFAULT_INSTRUCTION = "Add a genuinely new angle — do not simply agree with what was said."
_FALLBACK_INSTRUCTION = "Take the strongest opposing view to the last point and argue it hard."


class Moderator:
    """LLM-driven speaker selection with a round-robin safety net."""

    def __init__(self, chair: Speaker, roster: list[str]) -> None:
        if not roster:
            raise ValueError("moderator needs a non-empty roster")
        self._chair = chair
        self._roster = list(roster)

    async def next_turn(
        self, *, thread: str, round: int, last_speaker: str | None
    ) -> Turn:
        system = MODERATOR_SYSTEM.format(roster=", ".join(self._roster))
        user = (
            f"Round {round}. Discussion so far:\n\n{thread}\n\n"
            "Who speaks next, and what should they do?"
        )
        try:
            raw = await self._chair.speak(system=system, thread=user, temperature=0.4)
        except Exception:  # noqa: BLE001 — a chair failure must not stall the run
            return self._fallback(last_speaker)
        return self._parse(raw, last_speaker)

    def _parse(self, raw: str, last_speaker: str | None) -> Turn:
        match = _JSON.search(raw or "")
        if match:
            try:
                obj = json.loads(match.group(0))
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                speaker = str(obj.get("speaker", "")).strip()
                instruction = str(obj.get("instruction", "")).strip()
                if speaker in self._roster:
                    return Turn(speaker, instruction or _DEFAULT_INSTRUCTION)
        return self._fallback(last_speaker)

    def _fallback(self, last_speaker: str | None) -> Turn:
        """Round-robin: the next seat after whoever spoke last."""
        if last_speaker in self._roster:
            idx = (self._roster.index(last_speaker) + 1) % len(self._roster)
        else:
            idx = 0
        return Turn(self._roster[idx], _FALLBACK_INSTRUCTION)


# Structural check: Moderator satisfies the Chair protocol.
_: type[Chair] = Moderator
