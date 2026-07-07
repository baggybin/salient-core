"""The agent spawn/despawn observer seam (salient_core.daemon.set_spawn_observer).

Pins that the kernel fires a registered observer on agent spawn/despawn, that
the default is a no-op (kernel starts/stops nothing extra), and that a broken
skin observer is isolated from the spawn/stop path. Replaces the former
MSF-specific `_maybe_start_msf_watcher` daemon method — the offensive watcher
now lives skin-side behind this generic seam.
"""

from __future__ import annotations

import asyncio
import unittest

from salient_core.daemon import set_spawn_observer
from salient_core.daemon._runner_factory import _RunnerFactoryMixin


class _Daemon(_RunnerFactoryMixin):
    """Bare mixin instance — the notify helpers only touch the module-level
    observer and their args, so no full daemon is needed to exercise the seam."""


class SpawnObserverSeamTests(unittest.TestCase):
    def tearDown(self):
        set_spawn_observer(None)

    def test_default_no_observer_is_noop(self):
        d = _Daemon()
        d._notify_agent_spawn("a", {}, object())  # must not raise
        asyncio.run(d._notify_agent_despawn("a"))  # must not raise

    def test_on_spawn_fires_with_args(self):
        calls: list = []

        class Obs:
            def on_spawn(self, daemon, name, cfg, runner):
                calls.append((name, cfg))

        set_spawn_observer(Obs())
        _Daemon()._notify_agent_spawn("agentx", {"k": 1}, object())
        self.assertEqual(calls, [("agentx", {"k": 1})])

    def test_on_despawn_fires(self):
        calls: list = []

        class Obs:
            async def on_despawn(self, daemon, name):
                calls.append(name)

        set_spawn_observer(Obs())
        asyncio.run(_Daemon()._notify_agent_despawn("agy"))
        self.assertEqual(calls, ["agy"])

    def test_broken_on_spawn_is_isolated(self):
        class Obs:
            def on_spawn(self, *a):
                raise RuntimeError("boom")

        set_spawn_observer(Obs())
        # A broken observer must not stop an agent starting.
        _Daemon()._notify_agent_spawn("a", {}, object())

    def test_partial_observer_missing_despawn_is_noop(self):
        class Obs:
            def on_spawn(self, *a):
                pass

        set_spawn_observer(Obs())
        asyncio.run(_Daemon()._notify_agent_despawn("a"))  # no on_despawn → no-op


if __name__ == "__main__":
    unittest.main()
