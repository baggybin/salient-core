"""spawn_background holds a strong reference to fire-and-forget tasks so
they can't be garbage-collected mid-flight, and releases it on completion
so the tracking set stays bounded.
"""

from __future__ import annotations

import asyncio
import unittest

from salient_core.daemon._tasks import _BACKGROUND_TASKS, spawn_background


class SpawnBackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_runs_to_completion(self):
        ran = asyncio.Event()

        async def work():
            ran.set()

        task = spawn_background(work(), name="unit-work")
        # Held while in-flight.
        self.assertIn(task, _BACKGROUND_TASKS)
        await task
        await asyncio.sleep(0)  # let the done-callback fire
        self.assertTrue(ran.is_set())
        # Discarded once finished — no unbounded growth.
        self.assertNotIn(task, _BACKGROUND_TASKS)

    async def test_reference_held_when_caller_drops_it(self):
        # Simulate the fire-and-forget pattern: no local ref is kept.
        done = asyncio.Event()

        async def work():
            await asyncio.sleep(0.01)
            done.set()

        spawn_background(work())  # return value intentionally discarded
        # The only strong reference now lives in _BACKGROUND_TASKS.
        self.assertEqual(sum(1 for t in _BACKGROUND_TASKS if not t.done()), 1)
        await asyncio.wait_for(done.wait(), timeout=1.0)

    async def test_set_does_not_grow_across_many_spawns(self):
        async def noop():
            return None

        tasks = [spawn_background(noop()) for _ in range(50)]
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        # All discarded after completion.
        self.assertFalse(any(t in _BACKGROUND_TASKS for t in tasks))

    async def test_explicit_loop_is_used(self):
        loop = asyncio.get_running_loop()
        result = {}

        async def work():
            result["loop"] = asyncio.get_running_loop()

        task = spawn_background(work(), loop=loop, name="explicit-loop")
        await task
        self.assertIs(result["loop"], loop)


if __name__ == "__main__":
    unittest.main()
