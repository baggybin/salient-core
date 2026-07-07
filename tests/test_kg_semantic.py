"""KnowledgeGraph semantic recall — embedding storage + cosine ranking.

kg.py holds no embedder: the test embeds (via the deterministic MockEmbedder
from test_embeddings) then calls the sync kg helpers, mirroring how the daemon
backfill task / bus tool / runner will drive it.
"""

import tempfile
import unittest
from pathlib import Path

from salient_core.memory.embeddings import pack_vector
from salient_core.memory.kg import KnowledgeGraph
from tests._embed_mock import MockEmbedder


class KgSemanticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kg = KnowledgeGraph(Path(self.tmp.name) / "kg.db")
        self.emb = MockEmbedder()

    async def asyncTearDown(self):
        self.kg.close()
        self.tmp.cleanup()

    async def _embed_all(self):
        pending = self.kg.facts_needing_embedding(self.emb.model, limit=100)
        if not pending:
            return
        vecs = await self.emb.embed([t for _, t in pending])
        self.kg.store_embeddings(
            [(fid, pack_vector(v)) for (fid, _), v in zip(pending, vecs, strict=False)],
            self.emb.model,
        )

    async def test_semantic_ranks_relevant_first(self):
        self.kg.assert_fact("host:10.0.0.5", "vulnerable_to", "kerberos golden ticket forging")
        self.kg.assert_fact("wifi:corpnet", "captured", "wpa handshake deauth replay")
        self.kg.assert_fact("service:ldap", "supports", "kerberos authentication tickets")
        await self._embed_all()
        qv = await self.emb.embed_one("kerberos golden ticket")
        res = self.kg.semantic_query(qv, model=self.emb.model, top_k=3, min_score=0.0)
        self.assertTrue(res)
        self.assertIn("kerberos", res[0][0].object.lower())  # relevant fact ranks first
        self.assertNotIn("wifi", res[0][0].subject)  # not the irrelevant one
        self.assertGreaterEqual(res[0][1], res[-1][1])  # scores descending

    async def test_unembedded_facts_are_invisible_to_semantic(self):
        self.kg.assert_fact("a", "b", "c")  # never embedded
        qv = await self.emb.embed_one("anything at all")
        self.assertEqual(self.kg.semantic_query(qv, model=self.emb.model), [])

    async def test_needing_embedding_tracks_model(self):
        self.kg.assert_fact("a", "rel", "c")
        self.assertEqual(len(self.kg.facts_needing_embedding(self.emb.model)), 1)
        await self._embed_all()
        self.assertEqual(len(self.kg.facts_needing_embedding(self.emb.model)), 0)
        # A different model treats the existing vector as stale → needs re-embed.
        self.assertEqual(len(self.kg.facts_needing_embedding("other-model")), 1)

    async def test_embedding_counts_mirror_needing_predicate(self):
        # (total, embedded, pending) must partition the active set, and pending
        # must equal facts_needing_embedding's count verbatim — that's the
        # contract embeddings_status reports against.
        self.kg.assert_fact("a", "rel", "one")
        self.kg.assert_fact("b", "rel", "two")
        self.kg.assert_fact("c", "rel", "three")
        total, embedded, pending = self.kg.embedding_counts(self.emb.model)
        self.assertEqual((total, embedded, pending), (3, 0, 3))
        self.assertEqual(pending, len(self.kg.facts_needing_embedding(self.emb.model)))
        await self._embed_all()
        self.assertEqual(self.kg.embedding_counts(self.emb.model), (3, 3, 0))
        # Under a different model the existing vectors are stale → all pending,
        # mirroring facts_needing_embedding's NULL-or-wrong-model predicate.
        t2, e2, p2 = self.kg.embedding_counts("other-model")
        self.assertEqual((t2, e2, p2), (3, 0, 3))
        self.assertEqual(p2, len(self.kg.facts_needing_embedding("other-model")))

    async def test_expired_facts_excluded_from_semantic(self):
        import time

        self.kg.assert_fact("x", "y", "kerberos ticket", expires_at=time.time() - 1)
        await self._embed_all()  # embeds nothing (expired excluded from needing list)
        # Force-embed by re-asserting active, then expire post-embed:
        f = self.kg.assert_fact("x2", "y", "kerberos ticket")
        await self._embed_all()
        self.kg.store_embeddings([], self.emb.model)  # no-op guard
        qv = await self.emb.embed_one("kerberos ticket")
        before = self.kg.semantic_query(qv, model=self.emb.model)
        self.assertTrue(any(fact.id == f.id for fact, _ in before))
        # expire it and confirm it drops out
        self.kg.assert_fact("x2", "y", "kerberos ticket", expires_at=time.time() - 1)
        after = self.kg.semantic_query(qv, model=self.emb.model)
        self.assertFalse(any(fact.id == f.id for fact, _ in after))


if __name__ == "__main__":
    unittest.main()
