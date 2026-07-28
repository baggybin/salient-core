"""T2.4b Tier-2 — cgroup reaper: graceful degrade, proc_registry wiring, the
quiesce() cgroup branch, and a real systemd-run delegation smoke (skipped when
systemd --user delegation isn't available)."""

from __future__ import annotations

import shutil
import subprocess
import unittest

from salient_core.daemon import AgentRunner, proc_registry
from salient_core.daemon.cgroups import CgroupReaper, _safe


class _FakeReaper:
    """Duck-typed CgroupReaper for wiring tests (no real cgroup fs)."""

    def __init__(self) -> None:
        self.available = True
        self.ensured: list[str] = []
        self.placed: list[tuple[str, int]] = []
        self.killed: list[str] = []
        self.members_ret: dict[str, list[int]] = {}
        self.clear_on_kill = True

    def ensure_runner(self, name: str) -> None:
        self.ensured.append(name)

    def place(self, name: str, pid: int) -> bool:
        self.placed.append((name, pid))
        return True

    def members(self, name: str) -> list[int]:
        return list(self.members_ret.get(name, []))

    def kill_runner(self, name: str) -> bool:
        self.killed.append(name)
        if self.clear_on_kill:
            self.members_ret[name] = []
        return True

    def remove_runner(self, name: str) -> None:
        self.members_ret.pop(name, None)


class CgroupReaperUnitTests(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(_safe("red/lead nasty"), "red_lead_nasty")
        self.assertEqual(_safe(""), "_")

    def test_uninitialised_reaper_is_inert(self):
        r = CgroupReaper()
        self.assertFalse(r.available)
        # Every op must no-op / return falsy, never raise.
        r.ensure_runner("x")
        self.assertFalse(r.place("x", 1))
        self.assertEqual(r.members("x"), [])
        self.assertFalse(r.kill_runner("x"))
        r.remove_runner("x")

    def test_init_returns_bool_with_reason(self):
        # In an undelegated env init degrades (available False, reason set);
        # under delegation it may be True — either way it never raises and the
        # reason is populated.
        r = CgroupReaper()
        ok = r.init()
        self.assertIsInstance(ok, bool)
        self.assertEqual(ok, r.available)
        self.assertTrue(r.reason)


class ProcRegistryCgroupWiringTests(unittest.TestCase):
    def tearDown(self) -> None:
        proc_registry.set_cgroup_reaper(None)
        proc_registry.clear_runner("wire")

    def test_bind_and_register_drive_the_reaper(self):
        fake = _FakeReaper()
        proc_registry.set_cgroup_reaper(fake)
        tok = proc_registry.bind_runner("wire")
        try:
            proc_registry.register_subprocess(4321, wrapped=False, runner="wire")
        finally:
            proc_registry.unbind_runner(tok)
        self.assertIn("wire", fake.ensured)
        self.assertIn(("wire", 4321), fake.placed)

    def test_no_reaper_leaves_tier1_unaffected(self):
        proc_registry.set_cgroup_reaper(None)
        proc_registry.register_subprocess(4322, wrapped=False, runner="wire")
        self.assertIn(4322, {h.pid for h in proc_registry.registered("wire")})


class QuiesceCgroupBranchTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        proc_registry.set_cgroup_reaper(None)

    def _runner(self) -> AgentRunner:
        r = AgentRunner(name="cg", cfg={}, prompt_timeout=60.0, idle_timeout=0.0)
        r._task = None  # already terminal
        return r

    async def test_cgroup_reaped_reports_reaped_and_proven(self):
        fake = _FakeReaper()
        fake.members_ret["cg"] = [999999]  # a phantom member, cleared on kill
        proc_registry.set_cgroup_reaper(fake)
        rep = await self._runner().quiesce(grace=0.1, force=0.1)
        self.assertIn("cg", fake.killed)
        self.assertEqual(rep.cgroup_state, "reaped")
        self.assertEqual(rep.state, "proven_quiescent")

    async def test_cgroup_survivor_forces_unverified(self):
        fake = _FakeReaper()
        fake.clear_on_kill = False  # member outlives the kill
        fake.members_ret["cg"] = [999998]
        proc_registry.set_cgroup_reaper(fake)
        rep = await self._runner().quiesce(grace=0.1, force=0.1)
        self.assertEqual(rep.cgroup_state, "survivors")
        self.assertIn(999998, rep.tool_survived)
        self.assertEqual(rep.state, "unverified")

    async def test_empty_cgroup_is_proven(self):
        fake = _FakeReaper()  # no members
        proc_registry.set_cgroup_reaper(fake)
        rep = await self._runner().quiesce(grace=0.1, force=0.1)
        self.assertEqual(rep.cgroup_state, "empty")
        self.assertNotIn("cg", fake.killed)  # nothing to kill
        self.assertEqual(rep.state, "proven_quiescent")


_SYSTEMD_RUN = shutil.which("systemd-run")


@unittest.skipUnless(_SYSTEMD_RUN, "systemd-run not available")
class CgroupRealDelegationSmoke(unittest.TestCase):
    def test_real_place_kill_verify_under_delegation(self):
        src = "/home/jon/data/Projects/salient-core/src"
        script = (
            f"import sys; sys.path.insert(0, {src!r})\n"
            "from salient_core.daemon.cgroups import CgroupReaper\n"
            "import subprocess, time\n"
            "r = CgroupReaper()\n"
            "if not r.init():\n"
            "    print('SKIP_NO_DELEGATION'); raise SystemExit(0)\n"
            "r.ensure_runner('alpha')\n"
            "p = subprocess.Popen(['sleep','300'], start_new_session=True)\n"
            "assert r.place('alpha', p.pid)\n"
            "assert p.pid in r.members('alpha')\n"
            "assert r.kill_runner('alpha')\n"
            "time.sleep(0.3)\n"
            "assert r.members('alpha') == []\n"
            "assert p.poll() is not None\n"
            "r.remove_runner('alpha')\n"
            "print('REAL_REAP_OK')\n"
        )
        try:
            out = subprocess.run(
                [
                    _SYSTEMD_RUN,
                    "--user",
                    "-p",
                    "Delegate=yes",
                    "--scope",
                    "--quiet",
                    "python3",
                    "-c",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.skipTest(f"systemd-run --user unavailable: {exc}")
        combined = out.stdout + out.stderr
        if (
            "SKIP_NO_DELEGATION" in combined
            or out.returncode != 0
            and "REAL_REAP_OK" not in combined
        ):
            self.skipTest(f"delegation unavailable: {combined.strip()[:200]}")
        self.assertIn("REAL_REAP_OK", combined, msg=combined)


if __name__ == "__main__":
    unittest.main()
