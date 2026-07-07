"""salient.embeddings — provider-agnostic, inert-by-default embedding layer.

Pins: config resolution (profile + env + inert), pure-python cosine/top_k,
BLOB round-trip, and the per-text cache (a repeated text is not re-POSTed).
"""

import unittest
from unittest import mock

from salient_core.memory import embeddings as E
from tests._embed_mock import MockEmbedder
from tests._embed_mock import hash_embed as _hash_embed


class ResolveConfigTests(unittest.TestCase):
    def test_inert_when_unconfigured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(E.resolve_config(None))
            self.assertIsNone(E.resolve_config({}))
            self.assertIsNone(E.resolve_config({"embeddings": {}}))

    def test_profile_block(self):
        cfg = E.resolve_config({"embeddings": {"base_url": "http://x:11434/", "model": "nomic"}})
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.base_url, "http://x:11434")  # trailing slash trimmed
        self.assertEqual(cfg.model, "nomic")

    def test_enabled_false_is_inert(self):
        cfg = E.resolve_config(
            {"embeddings": {"base_url": "http://x", "model": "m", "enabled": False}}
        )
        self.assertIsNone(cfg)

    def test_env_fallback_and_envvar_expansion(self):
        env = {
            "SALIENT_EMBED_BASE_URL": "http://h:1/",
            "SALIENT_EMBED_MODEL": "m1",
            "MYKEY": "secret123",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            cfg = E.resolve_config({"embeddings": {"api_key": "${MYKEY}"}})
            self.assertEqual(cfg.base_url, "http://h:1")
            self.assertEqual(cfg.model, "m1")
            self.assertEqual(cfg.api_key, "secret123")


class VectorMathTests(unittest.TestCase):
    def test_pack_unpack_roundtrip(self):
        v = [0.5, -1.25, 3.0, 0.0]
        back = E.unpack_vector(E.pack_vector(v))
        self.assertEqual(len(back), len(v))
        for a, b in zip(v, back, strict=False):
            self.assertAlmostEqual(a, b, places=5)

    def test_unpack_garbage_is_none(self):
        self.assertIsNone(E.unpack_vector(None))
        self.assertIsNone(E.unpack_vector(b""))
        self.assertIsNone(E.unpack_vector(b"\x99nonsense"))

    def test_cosine_bounds(self):
        a = [1.0, 0.0, 0.0]
        self.assertAlmostEqual(E.cosine(a, a), 1.0, places=5)
        self.assertAlmostEqual(E.cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=5)
        self.assertEqual(E.cosine([], [1.0]), 0.0)  # mismatch/empty guarded

    def test_top_k_ranks_and_filters(self):
        q = _hash_embed("kerberos golden ticket attack")
        cands = [
            ("near", _hash_embed("golden ticket kerberos forging")),
            ("far", _hash_embed("wifi handshake capture deauth")),
            ("mid", _hash_embed("kerberos service ticket roasting")),
        ]
        ranked = E.top_k(q, cands, k=2, min_score=0.0)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][0], "near")  # most word overlap ranks first
        self.assertGreaterEqual(ranked[0][1], ranked[1][1])


class EmbedderCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_caches_repeated_text(self):
        emb = MockEmbedder()
        v1 = await emb.embed_one("hello world")
        v2 = await emb.embed_one("hello world")
        self.assertEqual(v1, v2)
        self.assertEqual(emb.post_calls, 1)  # second was a cache hit

    async def test_batch_dedupes_and_aligns(self):
        emb = MockEmbedder()
        out = await emb.embed(["a", "b", "a", "c"])
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0], out[2])  # aligned + same vector for "a"
        self.assertEqual(emb.posted[0], ["a", "b", "c"])  # deduped on the wire

    async def test_failure_returns_none(self):
        class Boom(E.Embedder):
            def __init__(self):
                super().__init__(E.EmbeddingConfig(model="m", base_url="http://x"))

            async def _post(self, inputs):
                return None

        self.assertIsNone(await Boom().embed(["x"]))


if __name__ == "__main__":
    unittest.main()
