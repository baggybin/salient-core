"""Bus lifecycle tools — swarm_finish.

Regression for the missing `import asyncio` in salient/bus/_lifecycle.py:
`swarm_finish` schedules teardown via `asyncio.create_task(...)`, which raised
`NameError` on the live path before the fix (no test exercised it, so the
suite stayed green while the path was broken).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from salient_core.bus import _delegation as deleg
from salient_core.bus._lifecycle import make_lifecycle_tools


def _text_of(res: dict) -> str:
    return " ".join(b.get("text", "") for b in (res.get("content") or []) if isinstance(b, dict))


class _Runner:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "idle"

    async def start(self) -> None:
        pass


class _SpawnDaemon:
    """Minimal daemon for spawn_template: caller trust + a runner table."""

    def __init__(self, caller_cfg: dict) -> None:
        self.all_cfgs = {"caller": caller_cfg}
        self.runners: dict = {}

    def _make_runner(self, cfg: dict) -> _Runner:
        return _Runner(cfg["name"])

    def _notify_agent_spawn(self, *a, **k) -> None:
        pass

    def _persist_running_agents(self) -> None:
        pass


class SpawnTemplateTrustTests(unittest.IsolatedAsyncioTestCase):
    """A1 (bus-runner bug hunt): spawn_template must gate on PER-TARGET trust
    (`_trust_covers`), not raw `bus_trusted` truthiness — a scoped list is truthy
    but not a licence to spawn any template."""

    def setUp(self) -> None:
        self._prev = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="spawn-tmpl-")
        os.chdir(self._tmp)
        (Path("templates")).mkdir()
        (Path("templates") / "planner.yaml").write_text(
            "name: planner\nteam: neutral\nsystem_prompt: a planner\n"
        )
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.addCleanup(os.chdir, self._prev)

    def _spawn_tool(self, daemon, owner):
        return next(t for t in make_lifecycle_tools(daemon, owner) if t.name == "spawn_template")

    async def test_scoped_trust_cannot_spawn_uncovered_template(self):
        d = _SpawnDaemon({"bus_trusted": ["someone_else"]})  # truthy, does NOT cover planner
        res = await self._spawn_tool(d, "caller").handler({"name": "planner"})
        self.assertIn("not trusted to spawn", _text_of(res))
        self.assertNotIn("planner", d.runners)  # nothing spawned

    async def test_trust_list_covering_target_spawns(self):
        d = _SpawnDaemon({"bus_trusted": ["planner"]})  # covers planner
        res = await self._spawn_tool(d, "caller").handler({"name": "planner"})
        self.assertNotIn("not trusted", _text_of(res))
        self.assertIn("planner", d.runners)

    async def test_blanket_trust_spawns(self):
        d = _SpawnDaemon({"bus_trusted": True})
        await self._spawn_tool(d, "caller").handler({"name": "planner"})
        self.assertIn("planner", d.runners)

    async def test_untrusted_caller_refused(self):
        d = _SpawnDaemon({})  # no bus_trusted at all
        res = await self._spawn_tool(d, "caller").handler({"name": "planner"})
        self.assertIn("not trusted to spawn", _text_of(res))
        self.assertNotIn("planner", d.runners)


class SpawnTemplateReserveTests(unittest.IsolatedAsyncioTestCase):
    """H4: spawn_template reserves the runner slot BEFORE the audit await, so two
    concurrent spawns can't orphan the first; it rolls the reservation back on
    failure and aborts if a killswitch sweeps during the await."""

    def setUp(self) -> None:
        self._prev = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="spawn-reserve-")
        os.chdir(self._tmp)
        Path("templates").mkdir()
        (Path("templates") / "planner.yaml").write_text(
            "name: planner\nteam: neutral\nsystem_prompt: a planner\n"
        )
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.addCleanup(os.chdir, self._prev)

    def _spawn_tool(self, daemon, owner="caller"):
        return next(t for t in make_lifecycle_tools(daemon, owner) if t.name == "spawn_template")

    async def test_concurrent_spawn_reserves_slot_and_refuses_second(self):
        d = _SpawnDaemon({"bus_trusted": True})
        gate = asyncio.Event()
        calls = {"n": 0}

        async def _fake_bypass(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                await gate.wait()  # first caller parks mid-await, slot reserved

        with patch.object(deleg, "_record_approval_bypass", _fake_bypass):
            tool = self._spawn_tool(d)
            t1 = asyncio.create_task(tool.handler({"name": "planner"}))
            # Wait until t1 has RESERVED the slot (and parked in the audit await).
            for _ in range(100):
                await asyncio.sleep(0)
                if "planner" in d.runners:
                    break
            r2 = await tool.handler({"name": "planner"})  # runs during t1's await
            gate.set()
            r1 = await t1

        self.assertEqual(list(d.runners.keys()), ["planner"], "exactly one runner")
        self.assertIn("already running", _text_of(r2), "the racing 2nd spawn must be refused")
        self.assertIn("spawned", _text_of(r1))

    async def test_start_failure_rolls_back_the_reservation(self):
        d = _SpawnDaemon({"bus_trusted": True})

        class _BadRunner(_Runner):
            async def start(self) -> None:
                raise RuntimeError("boom")

        d._make_runner = lambda cfg: _BadRunner(cfg["name"])
        res = await self._spawn_tool(d).handler({"name": "planner"})
        self.assertIn("failed to start", _text_of(res))
        self.assertNotIn(
            "planner", d.runners, "a failed start must not leave an orphan reservation"
        )

    async def test_spawn_aborts_when_killswitch_sweeps_during_the_await(self):
        d = _SpawnDaemon({"bus_trusted": True})

        async def _fake_bypass(*a, **k):
            d._stopping = True  # a killswitch swept while we were auditing

        with patch.object(deleg, "_record_approval_bypass", _fake_bypass):
            res = await self._spawn_tool(d).handler({"name": "planner"})
        self.assertIn("stopping", _text_of(res).lower())
        self.assertNotIn("planner", d.runners, "must not start a runner right after a STOP swept")


class SwarmFinishTests(unittest.IsolatedAsyncioTestCase):
    def _swarm_finish_tool(self, daemon, owner):
        tools = make_lifecycle_tools(daemon, owner)
        return next(t for t in tools if t.name == "swarm_finish")

    async def test_swarm_finish_schedules_teardown_without_crashing(self):
        owner = "red_lead"
        d = MagicMock()
        d._swarms = {owner: {"members": ["scanner", "nikto"]}}
        d._swarm_teardown = AsyncMock(return_value=None)

        tool = self._swarm_finish_tool(d, owner)
        res = await tool.handler({"reason": "task complete"})

        # Returns the scheduled-teardown ack, NOT a NameError / error result.
        self.assertNotIn("is_error", res)
        text = _text_of(res)
        self.assertIn("swarm_finish scheduled", text)
        self.assertIn("scanner", text)  # members listed

        # Let the scheduled task run; teardown was actually invoked.
        await asyncio.sleep(0)
        d._swarm_teardown.assert_awaited_once()

    async def test_swarm_finish_omitted_reason_still_tears_down(self):
        # @bus_tool migration must stay behavior-preserving: the old (unvalidated)
        # path let an OMITTED reason reach the handler, which coalesces ""→a
        # placeholder and tears down. `reason` is de-required (str="") precisely
        # so a teardown never fails over a missing label. If reason were required
        # again, model_validate would reject {} and this would return is_error.
        owner = "red_lead"
        d = MagicMock()
        d._swarms = {owner: {"members": ["scanner", "nikto"]}}
        d._swarm_teardown = AsyncMock(return_value=None)

        tool = self._swarm_finish_tool(d, owner)
        res = await tool.handler({})  # no reason supplied

        self.assertNotIn("is_error", res)
        self.assertIn("swarm_finish scheduled", _text_of(res))
        await asyncio.sleep(0)
        d._swarm_teardown.assert_awaited_once()

    async def test_swarm_finish_refused_for_non_orchestrator(self):
        d = MagicMock()
        d._swarms = {}  # caller is not a swarm orchestrator
        d._swarm_teardown = AsyncMock()

        tool = self._swarm_finish_tool(d, "loner")
        res = await tool.handler({"reason": "x"})

        self.assertTrue(res.get("is_error"))
        self.assertIn("only callable by SWARM", _text_of(res))
        d._swarm_teardown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
