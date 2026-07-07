"""Text-based semantic recall — the app-facing convenience over ``semantic_query``.

``KnowledgeGraph.semantic_query`` is deliberately embedder-free: it ranks a
pre-embedded query vector against stored vectors, so the caller must embed the
query text first. Every real call site repeats the same three-step dance
(``get_embedder`` → ``embed_one`` → ``semantic_query``); the canonical example
is the ``kg_semantic_query`` bus tool. This module lifts that dance into one
async helper so downstream apps don't reimplement (and mis-call) it — the tutor
did exactly that.

Degrades to ``[]`` when embeddings are unconfigured (no ``embeddings:`` profile
block / ``SALIENT_EMBED_*`` env), the embed call fails, or the query itself
errors — it never raises. Because ``[]`` is also the honest no-matches answer,
the degraded paths log a one-time warning so a misconfigured deployment is
distinguishable from an empty graph.
"""

from __future__ import annotations

import logging
from typing import Any

from .embeddings import get_embedder
from .kg import Fact, KnowledgeGraph

_log = logging.getLogger("salient.memory.recall")

_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        _log.warning(msg)


async def semantic_recall(
    kg: KnowledgeGraph,
    profile: dict[str, Any] | None,
    text: str | None,
    *,
    subject_prefix: str | None = None,
    top_k: int = 10,
    min_score: float = 0.5,
) -> list[tuple[Fact, float]]:
    """Embed ``text`` and return the top-``k`` semantically closest active facts.

    Returns ``[(Fact, score)]`` descending by score. Empty list when the query
    is blank/None, embeddings aren't configured, or the embedder returns nothing.
    ``subject_prefix`` scopes the search to one namespace (e.g. ``"pedagogy:"``).
    """
    if text is None or not text.strip():
        return []
    embedder = get_embedder(profile)
    if embedder is None:
        _warn_once(
            "unconfigured",
            "semantic_recall: embeddings are not configured (no `embeddings:` "
            "profile block / SALIENT_EMBED_* env) — every query returns []",
        )
        return []
    try:
        query_vec = await embedder.embed_one(text)
    except Exception:  # noqa: BLE001 — the docstring promises this never raises
        _warn_once(
            "embed-failed",
            "semantic_recall: the configured embedder raised while embedding "
            "the query (endpoint down or misconfigured?) — returning []",
        )
        return []
    if not query_vec:
        _warn_once(
            "embed-failed",
            "semantic_recall: the configured embedder returned no vector "
            "(endpoint down or misconfigured?) — returning []",
        )
        return []
    try:
        return kg.semantic_query(
            query_vec,
            model=embedder.model,
            top_k=top_k,
            min_score=min_score,
            subject_prefix=subject_prefix,
        )
    except Exception:
        _log.warning("semantic_recall: semantic_query failed", exc_info=True)
        return []
