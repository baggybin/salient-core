"""T3.1 spine (PR4) — the ASSEMBLED-prompt sha is stamped at bake and persisted.

Pins two contracts:
  1. bake: `runner._assembled_prompt_sha` == sha256 of the full assembled prompt
     the model runs under (`_augment_system_prompt(cfg)`), and it DIFFERS from
     the file-only `_prompt_sha` when the prompt is augmented (here: inheritance).
  2. store: the `jobs.assembled_prompt_sha` column is additive-migratable and a
     job row round-trips it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from salient_core.bus import ContextStore
from salient_core.daemon._runner_factory import _RunnerFactoryMixin
from salient_core.policy.scope import ScopeStore
from salient_core.providers import reset_provider_registry


class _BakeHarness(_RunnerFactoryMixin):
    def __init__(self, all_cfgs: dict[str, Any] | None = None) -> None:
        self.prompt_timeout = 60.0
        self.idle_timeout = 0.0
        self.tail_buffer_size = 100
        self.context = None
        self.profile: dict[str, Any] = {}
        self.event_hub = None
        self.engagement_path = None
        self.scope = ScopeStore(None, "test")
        self.actions = None
        self.listeners = None
        self.all_cfgs = all_cfgs or {}

    def _build_options(self, cfg, *, stderr_callback=None):  # stub — bake still runs
        del cfg, stderr_callback
        return ClaudeAgentOptions()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


class AssembledPromptShaBakeTests(unittest.TestCase):
    def test_stamp_matches_the_assembled_prompt(self) -> None:
        reset_provider_registry()
        d = _BakeHarness()
        cfg = {"name": "solo", "system_prompt": "OWN BODY"}
        r = d._make_runner(cfg)
        self.assertEqual(len(r._assembled_prompt_sha), 64)
        # The stamp IS the hash of what _augment_system_prompt produces.
        self.assertEqual(r._assembled_prompt_sha, _sha(d._augment_system_prompt(cfg)))

    def test_assembled_differs_from_file_only_when_augmented(self) -> None:
        reset_provider_registry()
        d = _BakeHarness(all_cfgs={"src": {"name": "src", "system_prompt": "INHERITED"}})
        cfg = {"name": "shadow", "system_prompt": "OWN", "inherit_system_prompt_from": "src"}
        r = d._make_runner(cfg)
        # _prompt_sha hashes only the authored file body (OWN); the assembled sha
        # covers inherited + own → they must differ.
        self.assertEqual(r._prompt_sha, _sha("OWN"))
        self.assertNotEqual(r._assembled_prompt_sha, r._prompt_sha)
        self.assertIn("INHERITED", d._augment_system_prompt(cfg))


class AssembledPromptShaStoreTests(unittest.TestCase):
    def test_pre_spine_jobs_table_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE jobs (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL,
                    job_id INTEGER NOT NULL, submitted_at REAL NOT NULL,
                    started_at REAL, finished_at REAL, prompt TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '', error TEXT, prompt_sha TEXT);
                """
            )
            conn.execute(
                "INSERT INTO jobs (agent, job_id, submitted_at, prompt) VALUES ('a', 1, 1.0, 'old')"
            )
            conn.commit()
            conn.close()

            store = ContextStore(path, events_cap_per_agent=0)
            try:
                self.assertFalse(store.degraded)
                with sqlite3.connect(path) as c:
                    cols = {row[1] for row in c.execute("PRAGMA table_info(jobs)")}
                    self.assertIn("assembled_prompt_sha", cols)
                    # legacy row survives, new column NULL
                    self.assertIsNone(
                        c.execute(
                            "SELECT assembled_prompt_sha FROM jobs WHERE job_id=1"
                        ).fetchone()[0]
                    )
            finally:
                store.close()

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "state.db")
            try:
                store.record_job(
                    agent="scout",
                    job_id=1,
                    prompt="p",
                    submitted_at=1.0,
                    started_at=1.0,
                    finished_at=2.0,
                    result="ok",
                    error=None,
                    prompt_sha="filesha",
                    assembled_prompt_sha="assembledsha",
                )
                row = store.load_recent_jobs("scout")[0]
                self.assertEqual(row["assembled_prompt_sha"], "assembledsha")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
