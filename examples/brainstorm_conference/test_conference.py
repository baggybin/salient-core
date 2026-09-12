"""Integration test for the round loop — fully offline against stubs.

No models, no network: stub panelists return canned text, a stub chair
round-robins the roster, and the deterministic HashEmbedder scores convergence.
Verifies the loop advances rounds, stops on convergence, respects max_rounds on
divergence, and calls the rapporteur.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402
from conference import run_conference  # noqa: E402
from forum import ForumStore  # noqa: E402
from roles import Report, Turn  # noqa: E402


class _StubPanelist:
    def __init__(self, label: str, text: str) -> None:
        self.label = label
        self._text = text
        self.calls = 0

    async def speak(self, *, system: str, thread: str, temperature: float | None = None) -> str:
        self.calls += 1
        return self._text


class _RoundRobinChair:
    """Cycles the roster deterministically, ignoring the thread."""

    def __init__(self, roster: list[str]) -> None:
        self._roster = roster

    async def next_turn(self, *, thread: str, round: int, last_speaker: str | None) -> Turn:
        if last_speaker in self._roster:
            idx = (self._roster.index(last_speaker) + 1) % len(self._roster)
        else:
            idx = 0
        return Turn(self._roster[idx], "say your piece")


class _StubRapporteur:
    def __init__(self) -> None:
        self.seen: str | None = None

    async def summarize(self, forum: ForumStore) -> Report:
        self.seen = forum.transcript()
        return Report(map_text="CONVERGED: caching. CRUX: none.")


def _panel(texts: dict[str, str]) -> dict[str, _StubPanelist]:
    return {label: _StubPanelist(label, text) for label, text in texts.items()}


def test_converges_early_when_panelists_agree() -> None:
    same = "memoise the hot path and cache results to skip recomputation"
    panel = _panel({"deepseek": same, "glm": same, "minimax": same})
    chair = _RoundRobinChair(list(panel))
    forum = ForumStore()

    result = asyncio.run(
        run_conference(
            seed="how to speed up the function",
            panelists=panel,
            moderator=chair,
            forum=forum,
            threshold=0.85,
            max_rounds=5,
        )
    )
    assert result.converged is True
    assert result.rounds == 1  # identical answers converge after the first round
    assert result.final_score is not None and result.final_score >= 0.85
    # every panelist spoke once; the seed + 3 posts are on the thread
    assert forum.count() == 4


def test_runs_to_max_rounds_when_divergent() -> None:
    panel = _panel(
        {
            "deepseek": "cache memoise recompute store hot path result",
            "glm": "parallelise threads worker pool concurrency scheduling latency",
            "minimax": "rewrite algorithm asymptotic complexity data structure bound",
        }
    )
    chair = _RoundRobinChair(list(panel))
    forum = ForumStore()

    result = asyncio.run(
        run_conference(
            seed="how to speed up the function",
            panelists=panel,
            moderator=chair,
            forum=forum,
            threshold=0.85,
            max_rounds=3,
        )
    )
    assert result.converged is False
    assert result.rounds == 3  # never converged ⇒ hits the cap
    assert forum.count() == 1 + 3 * 3  # seed + 3 speakers × 3 rounds


def test_rapporteur_is_called_with_full_thread() -> None:
    same = "cache the result to avoid recomputation on repeat calls"
    panel = _panel({"deepseek": same, "glm": same})
    chair = _RoundRobinChair(list(panel))
    forum = ForumStore()
    rap = _StubRapporteur()

    result = asyncio.run(
        run_conference(
            seed="speed",
            panelists=panel,
            moderator=chair,
            forum=forum,
            threshold=0.85,
            max_rounds=2,
            rapporteur=rap,
        )
    )
    assert result.report is not None
    assert "CONVERGED" in result.report.map_text
    assert rap.seen is not None and "TOPIC: speed" in rap.seen


def test_empty_panel_rejected() -> None:
    with pytest.raises(ValueError, match="at least one panelist"):
        asyncio.run(
            run_conference(
                seed="x", panelists={}, moderator=_RoundRobinChair([]), forum=ForumStore()
            )
        )


class _RecordingPanelist:
    """Records every thread it was shown, so tests can assert what it could see."""

    def __init__(self, label: str, text: str) -> None:
        self.label = label
        self._text = text
        self.threads: list[str] = []

    async def speak(self, *, system: str, thread: str, temperature: float | None = None) -> str:
        self.threads.append(thread)
        return self._text


def test_round_one_is_blind() -> None:
    panel = {
        "a": _RecordingPanelist("a", "alpha unique one two three"),
        "b": _RecordingPanelist("b", "beta different four five six"),
    }
    chair = _RoundRobinChair(list(panel))
    forum = ForumStore()
    asyncio.run(
        run_conference(
            seed="the topic",
            panelists=panel,
            moderator=chair,
            forum=forum,
            threshold=0.99,  # divergent answers won't converge, so round 2 runs
            max_rounds=2,
        )
    )
    # round 1 is blind: each panelist saw the topic but none of the peer's text
    assert "the topic" in panel["a"].threads[0]
    assert "beta different" not in panel["a"].threads[0]
    assert "alpha unique" not in panel["b"].threads[0]
    # round 2 reveals the committed round-1 positions
    assert "beta different" in panel["a"].threads[1]
