"""Tier-2 kernel-attested subprocess reaping via cgroup v2 (T2.4b).

When the daemon runs inside a *writable, delegated* cgroup v2 subtree (e.g.
launched under ``systemd-run --user -p Delegate=yes`` or a unit with
``Delegate=yes``), each runner gets its own child cgroup ``<D>/agents/<runner>``.
Tool subprocesses are placed into it at spawn; a killswitch reaps the whole
subtree with a single ``cgroup.kill`` write — which survives ``fork`` + ``setsid``
+ reparenting, the failure modes an in-process registry or a live-ancestry
``psutil`` scan miss — and can NEVER touch the daemon, which stays in ``D``.

Membership + ``cgroup.kill`` need no resource controllers, so we deliberately do
NOT touch ``cgroup.subtree_control``: that keeps the daemon able to live in ``D``
while ``D`` also has child cgroups (the "no internal processes" rule only binds
once a controller is enabled). Requires Linux ≥ 5.14 for ``cgroup.kill``.

Degrades cleanly: when the daemon is not in a writable delegated cgroup (the
common bare-launch case), ``available`` is False and the Tier-1 in-process proof
(proc_registry + os.kill(pid,0)) stands unchanged.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import suppress
from pathlib import Path

_log = logging.getLogger("salient.daemon.cgroups")

_CGROUP_MOUNT = Path("/sys/fs/cgroup")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(name: str) -> str:
    """Sanitize a runner name into a single cgroup directory component."""
    return _SAFE_NAME.sub("_", name) or "_"


class CgroupReaper:
    """Per-runner cgroup lifecycle + reaping. All ops are best-effort and never
    raise into the caller — a cgroup write failing degrades that runner's Tier-2
    proof, it does not break the killswitch."""

    def __init__(self) -> None:
        self.available = False
        self.reason = "not initialised"
        self._agents: Path | None = None  # <D>/agents

    def init(self) -> bool:
        """Detect a writable delegated cgroup v2 and set up ``<D>/agents``.
        Returns True iff Tier-2 reaping is live."""
        try:
            raw = Path("/proc/self/cgroup").read_text().strip()
        except OSError as exc:
            self.reason = f"cannot read /proc/self/cgroup: {exc}"
            return False
        # cgroup v2 is the single unified line "0::/the/path".
        v2 = next((ln for ln in raw.splitlines() if ln.startswith("0::")), None)
        if v2 is None:
            self.reason = "not cgroup v2 (no 0:: line)"
            return False
        rel = v2.split("::", 1)[1].strip()
        d = _CGROUP_MOUNT / rel.lstrip("/")
        if not os.access(d / "cgroup.procs", os.W_OK):
            self.reason = f"cgroup {d} not writable (no delegation)"
            return False
        # cgroup.kill (kernel >= 5.14) is the reap primitive; require it.
        if not (d / "cgroup.kill").exists() and not self._kill_supported(d):
            self.reason = "cgroup.kill unavailable (kernel < 5.14)"
            return False
        agents = d / "agents"
        try:
            agents.mkdir(exist_ok=True)
            # Prove we can create + remove a child (delegation is real).
            probe = agents / ".probe"
            probe.mkdir(exist_ok=True)
            probe.rmdir()
        except OSError as exc:
            self.reason = f"cannot create child cgroups under {agents}: {exc}"
            return False
        self._agents = agents
        self.available = True
        self.reason = f"delegated cgroup {d}"
        _log.info("cgroup Tier-2 reaping active: %s", self.reason)
        return True

    @staticmethod
    def _kill_supported(d: Path) -> bool:
        # cgroup.kill appears on child cgroups; the daemon's own leaf may not
        # list it until a child exists. Treat a child mkdir probe as the check.
        with suppress(OSError):
            probe = d / ".killprobe"
            probe.mkdir(exist_ok=True)
            ok = (probe / "cgroup.kill").exists()
            probe.rmdir()
            return ok
        return False

    def _runner_dir(self, name: str) -> Path | None:
        return None if self._agents is None else self._agents / _safe(name)

    def ensure_runner(self, name: str) -> None:
        """Create the runner's cgroup (idempotent). Called when a runner starts."""
        cg = self._runner_dir(name)
        if cg is None:
            return
        with suppress(OSError):
            cg.mkdir(exist_ok=True)

    def place(self, name: str, pid: int) -> bool:
        """Move a freshly-spawned tool subprocess into the runner cgroup. Future
        forks of it inherit membership; grandchildren already forked do not (the
        best-effort race the wrapper's pre-exec join closes)."""
        cg = self._runner_dir(name)
        if cg is None:
            return False
        try:
            cg.mkdir(exist_ok=True)
            (cg / "cgroup.procs").write_text(str(pid))
            return True
        except OSError as exc:
            _log.debug("cgroup place %s pid=%s failed: %s", name, pid, exc)
            return False

    def members(self, name: str) -> list[int]:
        """Live pids currently in the runner cgroup."""
        cg = self._runner_dir(name)
        if cg is None:
            return []
        try:
            return [int(x) for x in (cg / "cgroup.procs").read_text().split()]
        except OSError:
            return []

    def kill_runner(self, name: str) -> bool:
        """Atomically kill every process in the runner cgroup (and descendants),
        never touching the daemon. Returns True if the kill was issued."""
        cg = self._runner_dir(name)
        if cg is None:
            return False
        try:
            (cg / "cgroup.kill").write_text("1")
            return True
        except OSError as exc:
            _log.debug("cgroup.kill %s failed: %s", name, exc)
            return False

    def remove_runner(self, name: str) -> None:
        """Remove the (now-empty) runner cgroup. Best-effort — a non-empty or
        busy cgroup is left for the next sweep."""
        cg = self._runner_dir(name)
        if cg is None:
            return
        with suppress(OSError):
            cg.rmdir()
