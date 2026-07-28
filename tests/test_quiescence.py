"""T2.4b — provable STOP: proc_registry reaping, ReapableBackend, and the
runner.quiesce() escalation ladder + honest QuiescenceReport classification."""

from __future__ import annotations

import asyncio
import contextlib
import os
import unittest
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from salient_core.daemon import AgentRunner, proc_registry
from salient_core.daemon._backend import LocalClaudeBackend
from salient_core.daemon.runner import QuiescenceReport
from salient_core.protocols import AgentBackend, ReapableBackend


class _FakeBackend:
    """A complete AgentBackend that exposes no reapable child (like a remote
    backend / the codex path)."""

    def __init__(self, options: Any = None) -> None:
        self.options = options

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    async def receive_response(self):  # pragma: no cover - unused here
        if False:
            yield None

    async def interrupt(self) -> None: ...
    async def get_context_usage(self):
        return None

    def diagnose_failure(self, agent_name, error, stderr_tail) -> str:
        return ""


class _ReapableFake(_FakeBackend):
    def __init__(self, pid: int | None = None, alive: bool = False) -> None:
        super().__init__()
        self._pid, self._alive = pid, alive

    def child_pid(self) -> int | None:
        return self._pid

    def child_alive(self) -> bool:
        return self._alive


def _runner(**kw: Any) -> AgentRunner:
    return AgentRunner(name="q_test", cfg={}, prompt_timeout=60.0, idle_timeout=0.0, **kw)


async def _spawn(new_session: bool) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "sleep",
        "60",
        start_new_session=new_session,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


class ProcRegistryTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        proc_registry.clear_runner("R")

    async def _assert_reaped(self, proc: asyncio.subprocess.Process, *, wrapped: bool) -> None:
        proc_registry.register_subprocess(proc.pid, wrapped=wrapped, runner="R")
        self.assertTrue(any(h.pid == proc.pid for h in proc_registry.live_survivors("R")))
        signalled = proc_registry.reap_runner("R")
        self.assertIn(proc.pid, signalled)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)  # collect the zombie
        self.assertEqual(proc_registry.live_survivors("R"), [])

    async def test_reap_wrapped_kills_its_own_session_group(self):
        # start_new_session=True → own pgid == pid; reap via killpg is safe.
        await self._assert_reaped(await _spawn(True), wrapped=True)

    async def test_reap_unwrapped_is_single_pid_never_the_group(self):
        # Unwrapped shares the test's process group; reap must be os.kill(pid),
        # NOT killpg — a group signal here would hit the test runner itself.
        proc = await _spawn(False)
        my_pgid_before = os.getpgid(0)
        await self._assert_reaped(proc, wrapped=False)
        self.assertEqual(os.getpgid(0), my_pgid_before)  # test process survived

    async def test_deregister_drops_from_registry(self):
        proc = await _spawn(True)
        proc_registry.register_subprocess(proc.pid, wrapped=True, runner="R")
        proc_registry.deregister_subprocess(proc.pid, runner="R")
        self.assertEqual(proc_registry.registered("R"), [])
        proc.kill()
        await proc.wait()

    async def test_spawn_outside_a_runner_is_not_tracked(self):
        proc = await _spawn(True)
        proc_registry.register_subprocess(proc.pid, wrapped=True, runner=None)  # no bound runner
        self.assertEqual(proc_registry.known_pids(), set())
        proc.kill()
        await proc.wait()


class ReapableBackendTests(unittest.TestCase):
    def test_local_backend_is_reapable(self):
        b = LocalClaudeBackend(ClaudeAgentOptions())
        self.assertIsInstance(b, AgentBackend)
        self.assertIsInstance(b, ReapableBackend)

    def test_plain_backend_is_not_reapable(self):
        # A backend without child_pid/child_alive stays a valid AgentBackend but
        # is NOT a ReapableBackend → the runner degrades it to no_pid.
        self.assertIsInstance(_FakeBackend(), AgentBackend)
        self.assertNotIsInstance(_FakeBackend(), ReapableBackend)

    def test_local_backend_pid_is_none_before_connect(self):
        b = LocalClaudeBackend(ClaudeAgentOptions())
        self.assertIsNone(b.child_pid())
        self.assertFalse(b.child_alive())


class QuiesceLadderTests(unittest.IsolatedAsyncioTestCase):
    def _make(self, backend: Any) -> AgentRunner:
        r = _runner(backend_factory=lambda: backend)
        r._backend = backend
        return r

    async def test_graceful_exit_is_proven_quiescent(self):
        r = self._make(_FakeBackend())
        r._task = asyncio.create_task(asyncio.sleep(0))
        rep = await r.quiesce(grace=1.0, force=0.5)
        self.assertIsInstance(rep, QuiescenceReport)
        self.assertTrue(rep.task_done)
        self.assertFalse(rep.task_cancelled)
        self.assertEqual(rep.sdk_state, "no_pid")
        self.assertEqual(rep.state, "proven_quiescent")

    async def test_wedged_task_is_cancelled_then_proven(self):
        r = self._make(_FakeBackend())
        r._task = asyncio.create_task(asyncio.sleep(100))
        rep = await r.quiesce(grace=0.15, force=0.5)
        self.assertTrue(rep.task_cancelled)
        self.assertTrue(rep.task_done)  # cancel landed
        self.assertEqual(rep.state, "proven_quiescent")

    async def test_swallowed_cancel_is_force_reaped_not_faked(self):
        stop = asyncio.Event()

        async def wedged() -> None:
            while not stop.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.03)

        r = self._make(_FakeBackend())
        r._task = asyncio.create_task(wedged())
        rep = await r.quiesce(grace=0.1, force=0.3)
        self.assertTrue(rep.task_cancelled)
        self.assertFalse(rep.task_done)  # never exited
        self.assertEqual(rep.state, "force_reaped")
        stop.set()
        await r._task  # clean teardown

    async def test_live_sdk_child_forces_unverified(self):
        r = self._make(_ReapableFake(pid=os.getpid(), alive=True))  # our own pid: alive
        r._task = asyncio.create_task(asyncio.sleep(0))
        rep = await r.quiesce(grace=1.0, force=0.5)
        self.assertEqual(rep.sdk_state, "alive")
        self.assertEqual(rep.state, "unverified")

    async def test_dead_sdk_child_is_proven(self):
        r = self._make(_ReapableFake(pid=os.getpid(), alive=False))
        r._task = asyncio.create_task(asyncio.sleep(0))
        rep = await r.quiesce(grace=1.0, force=0.5)
        self.assertEqual(rep.sdk_state, "proven_dead")
        self.assertEqual(rep.state, "proven_quiescent")

    async def test_surviving_tool_subprocess_forces_unverified(self):
        # A registered tool subprocess that outlives the reap (simulated by
        # patching reap to a no-op) must force unverified, never proven.
        r = self._make(_FakeBackend())
        r._task = asyncio.create_task(asyncio.sleep(0))
        proc = await _spawn(True)
        proc_registry.register_subprocess(proc.pid, wrapped=True, runner=r.name)
        orig = proc_registry.reap_runner
        proc_registry.reap_runner = lambda name, **kw: []  # type: ignore[assignment]
        try:
            rep = await r.quiesce(grace=0.5, force=0.3)
        finally:
            proc_registry.reap_runner = orig  # type: ignore[assignment]
        self.assertIn(proc.pid, rep.tool_survived)
        self.assertEqual(rep.state, "unverified")
        proc.kill()
        await proc.wait()
        proc_registry.clear_runner(r.name)


if __name__ == "__main__":
    unittest.main()
