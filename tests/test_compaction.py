"""Unit tests for salient.compaction — the deterministic, reversible
compaction engine. Pins the accuracy-safety invariants the design rests on:
expired NON-credential facts are archived-then-purged; credential facts and
live facts are never touched; archive is written before any deletion; the
safety floor refuses to run on a near-empty KG.
"""

import json
import tempfile
import unittest
from pathlib import Path

from salient_core.memory import compaction
from salient_core.memory.kg import KnowledgeGraph


class _FakeCtx:
    """Minimal context-store stand-in: the engine only calls these two."""

    def __init__(self, job_keys=("job_1", "job_2"), other=("latest",)):
        self._entries = [{"key": k} for k in (*job_keys, *other)]
        self._job_keys = list(job_keys)

    def list_entries(self, agent=None):
        return list(self._entries)

    def gc_stale_job_keys(self):
        n = len(self._job_keys)
        self._entries = [e for e in self._entries if not e["key"].startswith("job_")]
        self._job_keys = []
        return n


def _kg(tmp):
    return KnowledgeGraph(Path(tmp) / "kg.db")


_PAST = 1.0  # epoch far in the past → expired
_FUTURE = 9_999_999_999.0  # far future → live


class CompactionEngineTests(unittest.TestCase):
    def _seed(self, kg):
        # live permanent, live future-expiry, expired non-cred, expired CRED
        kg.assert_fact("host:a", "runs_service", "smb")  # permanent live
        kg.assert_fact("host:b", "runs_service", "http", expires_at=_FUTURE)  # live
        kg.assert_fact("host:c", "had_banner", "old", expires_at=_PAST)  # expired junk
        kg.assert_fact(
            "user:admin", "has_password", "secret:password:x", expires_at=_PAST
        )  # expired CRED

    def test_survey_counts_only_expired_noncred(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            self._seed(kg)
            s = compaction.survey(kg, _FakeCtx())
            self.assertEqual(s["kg_expired_noncred"], 1)  # host:c only (cred excluded)
            self.assertEqual(s["context_job_keys"], 2)
            self.assertEqual(s["kg_active_facts"], 2)  # host:a + host:b

    def test_apply_purges_expired_noncred_keeps_creds_and_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            self._seed(kg)
            ctx = _FakeCtx()
            rep = compaction.apply(kg, ctx, archive_dir=Path(tmp) / "archive")
            self.assertTrue(rep["ok"])
            self.assertEqual(rep["kg_expired_purged"], 1)
            self.assertEqual(rep["context_job_keys_removed"], 2)

            # Live facts intact.
            self.assertTrue(kg.query(subject="host:a"))
            self.assertTrue(kg.query(subject="host:b"))
            # Expired CRED fact NOT purged (still present though expired).
            remaining_expired = kg.export_expired()
            self.assertEqual(len(remaining_expired), 1)
            self.assertEqual(remaining_expired[0]["predicate"], "has_password")

    def test_archive_written_before_purge_and_matches_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            self._seed(kg)
            rep = compaction.apply(kg, _FakeCtx(), archive_dir=Path(tmp) / "archive")
            ap = Path(rep["archive_path"])
            self.assertTrue(ap.exists())
            body = ap.read_text()
            import hashlib

            self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), rep["archive_sha256"])
            archived = json.loads(body)["kg_expired_noncred"]
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0]["subject"], "host:c")

    def test_safety_floor_refuses_on_near_empty_kg(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            kg.assert_fact("host:z", "had_banner", "x", expires_at=_PAST)  # 0 active
            rep = compaction.apply(kg, _FakeCtx(), archive_dir=Path(tmp) / "a", floor=1)
            self.assertFalse(rep["ok"])
            self.assertIn("safety floor", rep["error"])
            # Nothing purged — the expired fact is still there.
            self.assertEqual(len(kg.export_expired()), 1)

    def test_negative_floor_is_clamped_not_bypassed(self):
        # A negative floor must not crash or behave oddly — the engine clamps
        # it to 0 (the daemon rejects it outright at the command layer).
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            kg.assert_fact("host:a", "runs_service", "smb")
            kg.assert_fact("host:c", "had_banner", "x", expires_at=_PAST)
            rep = compaction.apply(kg, _FakeCtx(), archive_dir=Path(tmp) / "a", floor=-999)
            self.assertTrue(rep["ok"])
            self.assertEqual(rep["kg_expired_purged"], 1)


# NOTE: the cred-predicate sync check (compaction.CRED_PREDICATES vs the
# credentials module) moved to the salient-security package, which owns the
# credentials module. compaction.CRED_PREDICATES itself stays here.


class CurateTests(unittest.TestCase):
    """Operator-curated merge/dedupe: deletes the specific (permanent) duplicate
    facts the plan names, keeps survivors, refuses credentials, fail-safe on
    mismatch, archive-before-delete."""

    def _seed(self, kg):
        # survivor (permanent) + an alias-duplicate (permanent) + a credential
        # fact + an untouched live fact.
        kg.assert_fact("host:a", "version", "Splunk 7.0.2 detailed")  # survivor
        kg.assert_fact("host:a", "runs", "Splunk")  # dup → delete
        kg.assert_fact("user:admin", "has_password", "secret:password:x")
        kg.assert_fact("host:b", "runs_service", "http")  # untouched

    def _plan(self):
        return [
            {
                "survivor": {
                    "subject": "host:a",
                    "predicate": "version",
                    "object": "Splunk 7.0.2 detailed",
                },
                "deletes": [{"subject": "host:a", "predicate": "runs", "object": "Splunk"}],
                "reason": "predicate-alias duplicate",
            }
        ]

    def test_curate_deletes_approved_permanent_dup_keeps_survivor(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            self._seed(kg)
            rep = compaction.curate(kg, plan=self._plan(), archive_dir=Path(tmp) / "a")
            self.assertTrue(rep["ok"])
            self.assertEqual(rep["deleted"], 1)
            self.assertIsNone(kg.get_exact("host:a", "runs", "Splunk"))  # gone
            self.assertIsNotNone(kg.get_exact("host:a", "version", "Splunk 7.0.2 detailed"))  # kept
            self.assertTrue(Path(rep["archive_path"]).exists())

    def test_dry_run_mutates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            self._seed(kg)
            rep = compaction.curate(
                kg, plan=self._plan(), archive_dir=Path(tmp) / "a", dry_run=True
            )
            self.assertEqual(rep["would_delete"], 1)
            self.assertIsNotNone(kg.get_exact("host:a", "runs", "Splunk"))  # untouched

    def test_refuses_credential_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            self._seed(kg)
            plan = [
                {
                    "survivor": {
                        "subject": "host:a",
                        "predicate": "version",
                        "object": "Splunk 7.0.2 detailed",
                    },
                    "deletes": [
                        {
                            "subject": "user:admin",
                            "predicate": "has_password",
                            "object": "secret:password:x",
                        }
                    ],
                    "reason": "should be refused",
                }
            ]
            rep = compaction.curate(kg, plan=plan, archive_dir=Path(tmp) / "a")
            self.assertEqual(rep["deleted"], 0)
            self.assertEqual(len(rep["refused_credential"]), 1)
            self.assertIsNotNone(kg.get_exact("user:admin", "has_password", "secret:password:x"))

    def test_skips_group_when_survivor_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            self._seed(kg)
            plan = [
                {
                    "survivor": {"subject": "host:zz", "predicate": "nope", "object": "absent"},
                    "deletes": [{"subject": "host:a", "predicate": "runs", "object": "Splunk"}],
                    "reason": "survivor doesn't exist",
                }
            ]
            rep = compaction.curate(kg, plan=plan, archive_dir=Path(tmp) / "a")
            self.assertEqual(rep["deleted"], 0)
            self.assertEqual(rep["groups_skipped_no_survivor"], 1)
            self.assertIsNotNone(kg.get_exact("host:a", "runs", "Splunk"))  # not touched

    def test_mismatched_triple_is_failsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            self._seed(kg)
            plan = [
                {
                    "survivor": {
                        "subject": "host:a",
                        "predicate": "version",
                        "object": "Splunk 7.0.2 detailed",
                    },
                    "deletes": [
                        {"subject": "host:a", "predicate": "runs", "object": "Splnuk TYPO"}
                    ],  # mis-transcribed
                    "reason": "typo",
                }
            ]
            rep = compaction.curate(kg, plan=plan, archive_dir=Path(tmp) / "a")
            self.assertEqual(rep["deleted"], 0)
            self.assertEqual(len(rep["missing"]), 1)
            self.assertIsNotNone(kg.get_exact("host:a", "runs", "Splunk"))  # real one safe

    def test_malformed_plan_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            kg = _kg(tmp)
            self._seed(kg)
            rep = compaction.curate(kg, plan="not a list", archive_dir=Path(tmp) / "a")
            self.assertFalse(rep["ok"])
            self.assertIn("list", rep["error"])


if __name__ == "__main__":
    unittest.main()
