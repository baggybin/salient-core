"""Tests for the tutor learner-model scheduling write path on KnowledgeGraph.

These pin the two behaviours the spaced-repetition scheduler needs that the
ordinary kg_assert path can NOT provide:
  1. mastery (confidence) can be LOWERED — kg_assert's per-agent max-merge
     only ever raises it, so a lapse could never pull the estimate down;
  2. a topic migrating across the strong↔weak line leaves no stale twin row.
Plus the `review_due`-based due flag and prev-interval read the bus tool and
the web panel consume.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from salient_core.memory.kg import KnowledgeGraph

SUBJ = "learner:op"


class LearnerReviewTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.kg = KnowledgeGraph(Path(self._td.name) / "kg.db")
        self.addCleanup(self.kg.close)
        self.now = 1_000_000.0

    def test_record_creates_permanent_scheduled_fact(self):
        due = self.now + 6 * 86400
        f = self.kg.record_learner_review(
            SUBJ,
            "kerberoast",
            predicate="strong_topic",
            mastery=0.7,
            review_due=due,
            agent="tutor",
            now=self.now,
        )
        self.assertEqual(f.predicate, "strong_topic")
        self.assertEqual(f.confidence, 0.7)
        # Gradebook facts are permanent — never purged out from under a review.
        self.assertIsNone(f.expires_at)
        self.assertEqual(self.kg.purge_expired(now=self.now + 999 * 86400), 0)
        state = self.kg.learner_review_state(SUBJ, "kerberoast")
        self.assertIsNotNone(state)
        self.assertEqual(state["review_due"], due)

    def test_mastery_can_be_lowered(self):
        # The whole reason for a dedicated path: a lapse must pull mastery DOWN.
        self.kg.record_learner_review(
            SUBJ,
            "asrep",
            predicate="strong_topic",
            mastery=0.8,
            review_due=self.now + 10 * 86400,
            agent="tutor",
            now=self.now,
        )
        self.kg.record_learner_review(
            SUBJ,
            "asrep",
            predicate="weak_topic",
            mastery=0.4,
            review_due=self.now + 86400,
            agent="tutor",
            now=self.now + 5,
        )
        state = self.kg.learner_review_state(SUBJ, "asrep")
        self.assertEqual(state["mastery"], 0.4)
        self.assertEqual(state["predicate"], "weak_topic")

    def test_strong_weak_migration_leaves_no_twin(self):
        # Start weak, graduate to strong: the weak row must be gone, not orphaned.
        self.kg.record_learner_review(
            SUBJ,
            "dcsync",
            predicate="weak_topic",
            mastery=0.3,
            review_due=self.now + 86400,
            agent="tutor",
            now=self.now,
        )
        self.kg.record_learner_review(
            SUBJ,
            "dcsync",
            predicate="strong_topic",
            mastery=0.75,
            review_due=self.now + 8 * 86400,
            agent="tutor",
            now=self.now + 10,
        )
        weak = self.kg.query(subject=SUBJ, predicate="weak_topic", object_="dcsync")
        strong = self.kg.query(subject=SUBJ, predicate="strong_topic", object_="dcsync")
        self.assertEqual(len(weak), 0, "stale weak twin left behind")
        self.assertEqual(len(strong), 1)

    def test_prev_interval_derived_from_review_due(self):
        self.kg.record_learner_review(
            SUBJ,
            "ntlm",
            predicate="strong_topic",
            mastery=0.7,
            review_due=self.now + 6 * 86400,
            agent="tutor",
            now=self.now,
        )
        state = self.kg.learner_review_state(SUBJ, "ntlm")
        self.assertAlmostEqual(state["prev_interval_days"], 6.0, places=3)

    def test_first_review_has_no_prior_interval(self):
        self.assertIsNone(self.kg.learner_review_state(SUBJ, "never-seen"))

    def test_profile_flags_due_and_includes_misconceptions(self):
        # Overdue strong fact → due; future fact → not due.
        self.kg.record_learner_review(
            SUBJ,
            "overdue-topic",
            predicate="strong_topic",
            mastery=0.7,
            review_due=self.now - 86400,
            agent="tutor",
            now=self.now - 10 * 86400,
        )
        self.kg.record_learner_review(
            SUBJ,
            "future-topic",
            predicate="strong_topic",
            mastery=0.7,
            review_due=self.now + 86400,
            agent="tutor",
            now=self.now,
        )
        # A misconception, asserted the ordinary way (has expiry, no review_due).
        self.kg.assert_fact(
            SUBJ,
            "misconception",
            "thinks NTLM is Kerberos",
            agent="tutor",
            expires_at=self.now + 30 * 86400,
        )
        prof = self.kg.learner_profile(SUBJ, now=self.now)
        by_obj = {p["object"]: p for p in prof}
        self.assertTrue(by_obj["overdue-topic"]["due"])
        self.assertFalse(by_obj["future-topic"]["due"])
        self.assertIn("thinks NTLM is Kerberos", by_obj)
        # A misconception carries no schedule, so it's never "due".
        self.assertFalse(by_obj["thinks NTLM is Kerberos"]["due"])

    def test_profile_excludes_expired_nonpermanent_facts(self):
        self.kg.assert_fact(
            SUBJ,
            "misconception",
            "stale idea",
            agent="tutor",
            expires_at=self.now - 86400,  # already expired
        )
        prof = self.kg.learner_profile(SUBJ, now=self.now)
        self.assertNotIn("stale idea", {p["object"] for p in prof})


if __name__ == "__main__":
    unittest.main()
