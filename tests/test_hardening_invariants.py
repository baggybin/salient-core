"""Regression tests for the kernel-hardening pass — one invariant per review
finding. Each asserts a guarantee the kernel *claimed* but only enforced on the
dominant code path before this change.

Findings covered here (the cleanly unit-testable ones):
  1  built-in tools pass a real policy decision (not an implicit allow)
  2  delegation exposes an explicit detach opt-in (bounded child lifetime default)
  3  external cancellation of a runner task terminates it, not "idle timeout"
  4  one event subscriber can't corrupt another subscriber or a replay
  5  ContextStore cache doesn't diverge from disk after a failed write
  6  multi-fact compaction delete is all-or-nothing
  7  a registered PolicyDataset is deeply immutable
  9  a swallowed persistence/audit write flips an observable degraded-health flag
"""

from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path

from salient_core.bus import BusFlags
from salient_core.bus._context_store import ContextStore
from salient_core.daemon._event_hub import EventHub, fork_event
from salient_core.policy.decision import classify_builtin
from salient_core.policy.registry import PolicyDataset


class _FailingConn:
    """Proxy over a real sqlite3 connection that can be told to raise on the
    next ``execute`` (Finding 5/9) or on the Nth ``execute`` within a
    transaction (Finding 6). Delegates ``__enter__``/``__exit__`` so the real
    connection's commit/rollback semantics are preserved."""

    def __init__(self, real: sqlite3.Connection, *, fail_on_nth: int | None = None) -> None:
        self._real = real
        self.fail = False
        self.fail_on_nth = fail_on_nth
        self._n = 0

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def execute(self, *a, **k):
        self._n += 1
        if self.fail or (self.fail_on_nth is not None and self._n == self.fail_on_nth):
            raise sqlite3.OperationalError("simulated disk failure")
        return self._real.execute(*a, **k)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    @property
    def in_transaction(self):
        return self._real.in_transaction

    def close(self):
        return self._real.close()


# ── Finding 1 — built-in tools hit a real policy decision ──────────────────
class BuiltinPolicyDecisionTests(unittest.TestCase):
    def test_legacy_list_is_shadow_dispatch_compatibility_not_policy_allow(self):
        with self.assertWarnsRegex(DeprecationWarning, "shadow-only"):
            d = classify_builtin("Read", frozenset({"Read", "Bash"}))
        self.assertFalse(d.allow)
        self.assertTrue(d.dispatch_allowed)
        self.assertEqual(d.mode.value, "shadow")
        self.assertEqual(d.policy_class, "legacy_trusted_builtin")

    def test_unclassified_builtin_denied(self):
        with self.assertWarns(DeprecationWarning):
            d = classify_builtin("TodoWrite", frozenset({"Read"}))
        self.assertFalse(d.allow)
        self.assertFalse(d.dispatch_allowed)
        self.assertEqual(d.policy_class, "deny_unclassified")

    def test_empty_trust_denies_everything(self):
        # The old bug: a non-mcp__ tool was ALLOWED by default. Deny-by-default
        # is the whole point.
        with self.assertWarns(DeprecationWarning):
            decision = classify_builtin("Bash", frozenset())
        self.assertFalse(decision.allow)
        self.assertFalse(decision.dispatch_allowed)


# ── Finding 2 — delegation detach is an explicit opt-in ────────────────────
class DelegationDetachFlagTests(unittest.TestCase):
    def test_detach_defaults_false(self):
        # Bounded child lifetime (reap on timeout/cancel) is the default;
        # fire-and-forget must be asked for explicitly.
        self.assertFalse(BusFlags().detach)

    def test_detach_opt_in(self):
        self.assertTrue(BusFlags(detach=True).detach)


# ── Finding 3 — external runner cancellation terminates, not "idle timeout" ─
class RunnerCancellationAttributionTests(unittest.IsolatedAsyncioTestCase):
    def _make_runner(self):
        from salient_core.daemon import AgentRunner

        return AgentRunner(
            name="child",
            cfg={},
            prompt_timeout=60.0,  # watchdog exists but won't fire in-test
            idle_timeout=0.0,
        )

    async def test_external_cancel_is_not_reclassified_as_idle_timeout(self):
        from salient_core.daemon._helpers import Job

        r = self._make_runner()
        job = Job(id=1, prompt="x", submitted_at=0.0)

        async def _blocking(_job):
            await asyncio.Event().wait()  # never completes on its own

        r._process = _blocking  # type: ignore[method-assign]
        task = asyncio.create_task(r._run_job(job))
        await asyncio.sleep(0.05)  # let it reach `await proc_task`
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # The bug converted this external cancel into a false idle timeout and
        # left the runner alive. Now it's attributed to the stop and re-raised.
        self.assertEqual(job.error, "runner stopping")
        self.assertNotIn("idle timeout", (job.error or ""))


# ── Finding 4 — event isolation across subscribers and replay ──────────────
class EventIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscriber_mutation_does_not_corrupt_others_or_replay(self):
        hub = EventHub()
        evt = {"agent": "a", "kind": "tool-call", "seq": 1, "meta": {"nested": {"x": 1}}}
        q1, _ = hub.subscribe()
        q2, _ = hub.subscribe()
        hub.publish(evt)
        e1 = await asyncio.wait_for(q1.get(), 1)
        e2 = await asyncio.wait_for(q2.get(), 1)

        e1["meta"]["nested"]["x"] = 999  # one subscriber annotates its frame

        self.assertEqual(e2["meta"]["nested"]["x"], 1, "other subscriber corrupted")
        _q3, snap = hub.subscribe()
        self.assertEqual(snap[0]["meta"]["nested"]["x"], 1, "replay corrupted")
        self.assertEqual(evt["meta"]["nested"]["x"], 1, "canonical ring object corrupted")

    def test_fork_event_deep_copies_meta_but_stays_plain_dict(self):
        evt = {"agent": "a", "seq": 1, "meta": {"n": {"x": 1}}}
        f = fork_event(evt)
        f["meta"]["n"]["x"] = 2
        self.assertEqual(evt["meta"]["n"]["x"], 1)
        self.assertIsInstance(f, dict)
        self.assertIsInstance(f["meta"], dict)  # json.dumps-able, no proxies


# ── Finding 5 — ContextStore commit-first (no cache divergence) ────────────
class ContextStoreCommitFirstTests(unittest.TestCase):
    def test_failed_write_leaves_cache_consistent_with_disk(self):
        with self._store() as store:
            store.write("agent", "k", "v1")
            store._conn.fail = True  # type: ignore[attr-defined]
            with self.assertRaises(sqlite3.Error):
                store.write("agent", "k", "v2")
            # Old bug: cache was mutated BEFORE the commit, so read() returned
            # the unpersisted "v2". Commit-first keeps the cache at "v1".
            self.assertEqual(store.read("agent", "k"), "v1")

    def test_failed_meta_set_leaves_cache_consistent(self):
        with self._store() as store:
            store.meta_set("mk", "m1")
            store._conn.fail = True  # type: ignore[attr-defined]
            with self.assertRaises(sqlite3.Error):
                store.meta_set("mk", "m2")
            self.assertEqual(store.meta_get("mk"), "m1")

    class _StoreCtx:
        def __init__(self, tmp: Path):
            self.tmp = tmp

        def __enter__(self):
            self.store = ContextStore(self.tmp / "t.db")
            self.store._conn = _FailingConn(self.store._conn)  # type: ignore[assignment]
            return self.store

        def __exit__(self, *exc):
            self.store.close()

    def _store(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        return self._StoreCtx(Path(self._td.name))


# ── Finding 6 — compaction delete is all-or-nothing ────────────────────────
class CompactionAtomicityTests(unittest.TestCase):
    def test_delete_many_rolls_back_on_mid_set_failure(self):
        import tempfile

        from salient_core.memory.kg import KnowledgeGraph

        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            try:
                ids = [kg.assert_fact(f"s{i}", "p", f"o{i}").id for i in range(3)]
                before = kg.stats().get("total_facts", 0)
                self.assertEqual(before, 3)
                # Fail on the 2nd DELETE inside the single transaction: the whole
                # set must roll back (not leave fact 0 deleted, 1/2 kept).
                real = kg._conn
                kg._conn = _FailingConn(real, fail_on_nth=2)  # type: ignore[assignment]
                with self.assertRaises(sqlite3.Error):
                    kg.delete_many(ids)
                kg._conn = real  # type: ignore[assignment]
                self.assertEqual(
                    kg.stats().get("total_facts", 0),
                    3,
                    "partial delete leaked — transaction was not atomic",
                )
            finally:
                kg.close()

    def test_delete_many_happy_path_removes_all(self):
        import tempfile

        from salient_core.memory.kg import KnowledgeGraph

        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            try:
                ids = [kg.assert_fact(f"s{i}", "p", f"o{i}").id for i in range(3)]
                self.assertEqual(kg.delete_many(ids), 3)
                self.assertEqual(kg.stats().get("total_facts", 0), 0)
            finally:
                kg.close()


# ── Finding 7 — PolicyDataset is deeply immutable ──────────────────────────
class PolicyDatasetImmutabilityTests(unittest.TestCase):
    def test_mutating_original_input_does_not_reach_registered_policy(self):
        patterns = {"toolX": [("label", "regex")]}
        ds = PolicyDataset(tool_targets={}, prohibited_patterns=patterns, loud_patterns={})
        patterns["toolX"].append(("sneaky", ".*"))  # mutate the ORIGINAL list
        self.assertEqual(
            list(ds.prohibited_patterns["toolX"]),
            [("label", "regex")],
            "deep mutation of the input reached live policy",
        )

    def test_registered_mapping_is_read_only(self):
        ds = PolicyDataset(
            tool_targets={}, prohibited_patterns={"t": [("l", "r")]}, loud_patterns={}
        )
        with self.assertRaises(TypeError):
            ds.prohibited_patterns["new"] = [("x", "y")]  # type: ignore[index]


# ── Finding 9 — persistence failure is observable, not silent ──────────────
class DegradedHealthTests(unittest.TestCase):
    def test_swallowed_audit_write_flips_health_flag(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "t.db")
            try:
                store._conn = _FailingConn(store._conn)  # type: ignore[assignment]
                self.assertFalse(store.degraded)
                store._conn.fail = True  # type: ignore[attr-defined]
                # record_job swallows the error (agents keep running) — but it
                # must now be OBSERVABLE, not silent.
                store.record_job("a", 1, "p", 0.0, None, None, "r", None)
                self.assertTrue(store.degraded)
                self.assertFalse(store.health()["ok"])
                self.assertEqual(store.health()["dropped_writes"], 1)
                self.assertIn("job", store.health()["reason"])
            finally:
                store.close()

    def test_health_identifies_each_failed_sink_and_count(self):
        # Every swallowed persistence class — including the migration and prune
        # paths that used to `pass`/`return 0` silently — must flip health AND be
        # attributed by sink with a count (Finding 6).
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            store = ContextStore(Path(td) / "t.db", events_cap_per_agent=100)
            try:
                store._conn = _FailingConn(store._conn)  # type: ignore[assignment]
                store._conn.fail = True  # type: ignore[attr-defined]

                store._migrate_questions_answered_by()  # -> "migration"
                store._migrate_jobs_prompt_sha()  # -> "migration" (2nd)
                store._prune_events()  # -> "prune"
                store.record_job("a", 1, "p", 0.0, None, None, "r", None)  # -> "job"

                h = store.health()
                self.assertFalse(h["ok"])
                self.assertTrue(h["degraded"])
                self.assertEqual(h["sinks"]["migration"], 2)
                self.assertEqual(h["sinks"]["prune"], 1)
                self.assertEqual(h["sinks"]["job"], 1)
                self.assertEqual(h["dropped_writes"], 4)
            finally:
                store.close()


# ── Finding 2 (follow-up) — cancel_job is idempotent (no double interrupt) ──
class CancelJobIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_double_cancel_interrupts_once(self):
        from salient_core.daemon import AgentRunner
        from salient_core.daemon._helpers import Job

        class _SpyBackend:
            def __init__(self):
                self.interrupts = 0

            async def interrupt(self):
                self.interrupts += 1

        r = AgentRunner(name="child", cfg={}, prompt_timeout=60.0)
        spy = _SpyBackend()
        r._backend = spy
        r._turn_active = True
        r.current = Job(id=7, prompt="x", submitted_at=0.0)

        # Two independent reap owners target the same in-flight child job.
        self.assertTrue(await r.cancel_job(7))
        self.assertTrue(await r.cancel_job(7))
        self.assertEqual(spy.interrupts, 1, "same job interrupted twice — not idempotent")


# ── Finding 10 — resource ownership: stores close cleanly, no leaked handles ─
class ResourceOwnershipTests(unittest.TestCase):
    def test_context_store_context_manager_closes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            with ContextStore(Path(td) / "t.db") as store:
                store.write("a", "k", "v")
                self.assertIsNotNone(store._conn)
            self.assertIsNone(store._conn, "ContextStore left its SQLite handle open")

    def test_kg_context_manager_closes(self):
        import tempfile

        from salient_core.memory.kg import KnowledgeGraph

        with tempfile.TemporaryDirectory() as td:
            with KnowledgeGraph(Path(td) / "kg.db") as kg:
                kg.assert_fact("s", "p", "o")
                self.assertIsNotNone(kg._conn)
            self.assertIsNone(kg._conn, "KnowledgeGraph left its SQLite handle open")

    def test_spawn_background_task_clears_after_completion(self):
        # A tracked fire-and-forget task must not linger in the strong-ref set
        # after it finishes — else the set (and its work) leaks past shutdown.
        import asyncio

        from salient_core.daemon._tasks import _BACKGROUND_TASKS, spawn_background

        async def _run():
            done = asyncio.Event()

            async def _work():
                done.set()

            spawn_background(_work())
            await done.wait()
            await asyncio.sleep(0)  # let the done-callback discard it
            return len(_BACKGROUND_TASKS)

        self.assertEqual(asyncio.run(_run()), 0)


if __name__ == "__main__":
    unittest.main()
