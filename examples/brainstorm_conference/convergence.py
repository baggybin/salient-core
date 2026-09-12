"""Convergence scoring — the deterministic half of the hybrid moderator.

The chair (an LLM) decides *who speaks*; this decides *when it's over*, on a
measured signal rather than the chair's vibe. It wraps the kernel's own
``semantic_agreement`` — the exact embedding-cosine measure ``ask_consensus``
uses — so "have they converged?" is answered the same way the kernel answers it
everywhere else.

Ships a deterministic offline ``HashEmbedder`` (the same one the consensus_panel
example uses) so scoring needs no embedding API or network; swap in the kernel's
real ``salient_core.memory.embeddings.Embedder`` for live semantic quality.
"""

from __future__ import annotations

import hashlib
import re

from salient_core.bus._consensus import semantic_agreement

_WORD = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    """Bag-of-words vectors in a fixed-dim space via stable hashing. Similar
    texts point the same way, so the kernel's cosine-based ``semantic_agreement``
    returns a meaningful score with no embedding API. The async ``embed`` matches
    the ``Embedder`` shape the scorer expects."""

    def __init__(self, dim: int = 96) -> None:
        self.dim = dim
        self.model = "hash-embedder-v1"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _WORD.findall((text or "").lower()):
            h = int(hashlib.sha1(tok.encode()).hexdigest(), 16) % self.dim
            v[h] += 1.0
        return v

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


async def convergence_score(
    contributions: dict[str, str], embedder: object | None = None
) -> float | None:
    """How much the latest contributions agree *in meaning*, in ``[0, 1]``.

    ``contributions`` maps speaker label → their most recent text. Returns
    ``None`` when fewer than two usable contributions exist (the caller then
    keeps going — you can't be converged with one voice). Never raises.
    """
    return await semantic_agreement(contributions, embedder or HashEmbedder())
