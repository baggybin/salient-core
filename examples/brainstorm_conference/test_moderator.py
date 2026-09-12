"""Offline tests for the chair: JSON parsing + round-robin fallback."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402
from moderator import Moderator  # noqa: E402


class _StubChair:
    """A fake LLM chair that returns a scripted reply (or raises)."""

    label = "chair"

    def __init__(self, reply: str | None = None, *, raises: bool = False) -> None:
        self._reply = reply
        self._raises = raises

    async def speak(
        self, *, system: str, thread: str, temperature: float | None = None
    ) -> str:
        if self._raises:
            raise RuntimeError("model down")
        return self._reply or ""


ROSTER = ["deepseek", "glm", "minimax"]


def _turn(chair: _StubChair, last: str | None = None):
    mod = Moderator(chair, ROSTER)
    return asyncio.run(mod.next_turn(thread="...", round=1, last_speaker=last))


def test_valid_json_picks_named_speaker() -> None:
    turn = _turn(_StubChair('{"speaker": "glm", "instruction": "push back hard"}'))
    assert turn.speaker == "glm"
    assert turn.instruction == "push back hard"


def test_json_embedded_in_prose_is_extracted() -> None:
    reply = 'Sure — here is my call:\n{"speaker": "minimax", "instruction": "go bold"}\nthanks'
    turn = _turn(_StubChair(reply))
    assert turn.speaker == "minimax"


def test_speaker_not_in_roster_falls_back() -> None:
    turn = _turn(_StubChair('{"speaker": "gpt-9", "instruction": "x"}'), last="deepseek")
    assert turn.speaker == "glm"  # round-robin after deepseek


def test_garbage_reply_falls_back() -> None:
    turn = _turn(_StubChair("no json here at all"), last="glm")
    assert turn.speaker == "minimax"  # round-robin after glm


def test_chair_exception_falls_back() -> None:
    turn = _turn(_StubChair(raises=True), last="minimax")
    assert turn.speaker == "deepseek"  # wraps around


def test_no_last_speaker_starts_at_first() -> None:
    turn = _turn(_StubChair("garbage"), last=None)
    assert turn.speaker == "deepseek"


def test_empty_roster_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty roster"):
        Moderator(_StubChair(), [])
