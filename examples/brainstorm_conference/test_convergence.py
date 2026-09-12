"""Offline tests for convergence scoring (deterministic HashEmbedder)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from convergence import HashEmbedder, convergence_score  # noqa: E402


def test_identical_contributions_score_high() -> None:
    same = "cache the result and memoise the hot path to avoid recomputation"
    score = asyncio.run(convergence_score({"a": same, "b": same, "c": same}))
    assert score is not None and score > 0.95  # identical ⇒ near 1.0


def test_disjoint_contributions_score_lower() -> None:
    contributions = {
        "a": "cache memoise recompute hot path store result",
        "b": "parallelise threads worker pool concurrency scheduling",
        "c": "rewrite algorithm asymptotic complexity data structure",
    }
    score = asyncio.run(convergence_score(contributions))
    assert score is not None
    # disjoint vocabularies ⇒ well below a convergence threshold
    assert score < 0.5


def test_single_contribution_is_none() -> None:
    assert asyncio.run(convergence_score({"a": "only one voice"})) is None


def test_empty_is_none() -> None:
    assert asyncio.run(convergence_score({})) is None


def test_hash_embedder_shape() -> None:
    emb = HashEmbedder(dim=32)
    vecs = asyncio.run(emb.embed(["hello world", "hello there"]))
    assert len(vecs) == 2
    assert all(len(v) == 32 for v in vecs)
