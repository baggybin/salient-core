"""The idea leaderboard — the surprise detector.

Fixes the gotcha we flagged early: a raw tally counts *words*, not *thoughts*, so
"shard by tenant" and "partition per customer" look like two separate ideas. Here
we embed every contribution and greedily cluster the ones that mean the same
thing, then rank each cluster by the number of *distinct* panelists who backed it.
Cross-model support is the signal you can't get from one model alone — an idea two
rivals independently reached is worth more than one model's pet.

Pure and offline via the deterministic ``HashEmbedder``; pass the kernel's real
``Embedder`` for live semantic quality. (A durable, cross-session version would
assert each cluster into the kernel KG, whose noisy-OR corroboration is the same
idea persisted — left as a follow-up.)
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from convergence import HashEmbedder
from roles import IdeaCluster

from salient_core.memory.embeddings import cosine


def _short(text: str, limit: int = 90) -> str:
    """A compact label: the first sentence, trimmed."""
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    first = " ".join(first.split())
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _mean(vecs: list[list[float]]) -> list[float]:
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(v[i] for v in vecs) / n for i in range(dim)]


async def build_leaderboard(
    posts: Sequence[tuple[str, str]],
    embedder: object | None = None,
    *,
    threshold: float = 0.6,
    min_support: int = 1,
) -> list[IdeaCluster]:
    """Cluster ``(speaker, text)`` posts by meaning; rank by distinct supporters.

    ``threshold`` is the cosine at/above which two contributions are "the same
    idea". ``min_support`` drops clusters with fewer distinct backers (set 2 to
    show only ideas that got genuine cross-model corroboration).
    """
    emb = embedder or HashEmbedder()
    texts = [t for _, t in posts]
    if not texts:
        return []
    vecs = await emb.embed(texts)

    clusters: list[dict] = []
    for (speaker, text), vec in zip(posts, vecs, strict=False):
        if not vec or not any(x != 0.0 for x in vec):
            continue
        best: dict | None = None
        best_sim = 0.0
        for cluster in clusters:
            sim = cosine(vec, cluster["centroid"])
            if sim > best_sim:
                best_sim, best = sim, cluster
        if best is not None and best_sim >= threshold:
            best["vecs"].append(vec)
            best["supporters"].add(speaker)
            best["centroid"] = _mean(best["vecs"])
        else:
            clusters.append(
                {"centroid": vec, "vecs": [vec], "rep": text, "supporters": {speaker}}
            )

    ranked = [
        IdeaCluster(label=_short(c["rep"]), supporters=tuple(sorted(c["supporters"])))
        for c in clusters
        if len(c["supporters"]) >= min_support
    ]
    ranked.sort(key=lambda idea: idea.support, reverse=True)
    return ranked
