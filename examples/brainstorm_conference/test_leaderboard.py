"""Offline tests for the idea leaderboard (deterministic HashEmbedder)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from leaderboard import build_leaderboard  # noqa: E402


def _run(posts, **kw):
    return asyncio.run(build_leaderboard(posts, **kw))


def test_similar_ideas_from_different_speakers_cluster() -> None:
    posts = [
        ("deepseek", "cache the result and memoise the hot path to avoid recompute"),
        ("glm", "memoise the hot path and cache results to skip recompute"),
        ("minimax", "parallelise across threads with a worker pool for concurrency"),
    ]
    board = _run(posts)
    top = board[0]
    assert top.support == 2  # deepseek + glm backed the same idea
    assert set(top.supporters) == {"deepseek", "glm"}
    assert board[-1].support == 1  # the parallelism idea stands alone


def test_same_speaker_twice_counts_once() -> None:
    posts = [
        ("deepseek", "cache memoise hot path recompute store result value"),
        ("deepseek", "cache memoise hot path recompute store result value again"),
    ]
    board = _run(posts, threshold=0.5)
    assert len(board) == 1
    assert board[0].support == 1  # distinct supporters, not post count


def test_min_support_filters_singletons() -> None:
    posts = [
        ("a", "alpha beta gamma delta unique one"),
        ("b", "epsilon zeta eta theta unique two"),
    ]
    board = _run(posts, min_support=2)
    assert board == []  # nothing got cross-model support


def test_ranked_by_support_descending() -> None:
    shared = "cache memoise hot path recompute"
    posts = [
        ("a", shared),
        ("b", shared),
        ("c", shared),
        ("d", "parallelise threads worker pool concurrency scheduling"),
    ]
    board = _run(posts)
    assert [idea.support for idea in board] == sorted(
        [idea.support for idea in board], reverse=True
    )
    assert board[0].support == 3


def test_empty_posts() -> None:
    assert _run([]) == []
