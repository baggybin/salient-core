"""Shared deterministic, network-free embedder for memory tests.

Not a test module (leading underscore — pytest won't collect it). Bag-of-words
hashing so texts sharing words get higher cosine — enough to assert ranking
without a real embedding model.
"""

import hashlib

from salient_core.memory import embeddings as E

_DIM = 64


def hash_embed(text: str) -> list[float]:
    vec = [0.0] * _DIM
    for tok in text.lower().split():
        h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)
        vec[h % _DIM] += 1.0
    return vec


class MockEmbedder(E.Embedder):
    """Deterministic embedder; records POST calls so tests can assert caching."""

    def __init__(self):
        super().__init__(E.EmbeddingConfig(model="mock", base_url="http://mock"))
        self.post_calls = 0
        self.posted: list[list[str]] = []

    async def _post(self, inputs):
        self.post_calls += 1
        self.posted.append(list(inputs))
        return [hash_embed(t) for t in inputs]
