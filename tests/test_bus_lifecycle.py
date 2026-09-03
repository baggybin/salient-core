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
from unittest.mock import AsyncMock, MagicMock

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
