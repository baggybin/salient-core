"""Per-runner subprocess registry — the generic quiescence mechanism (T2.4b).

A runner is an in-process ``asyncio`` Task with no OS presence, so "prove the
runner is dead" reduces to "prove the subprocesses its tools spawned are dead".
The kernel owns this registry (generic mechanism); the security skin's tool
spawn funnel is the only thing that *populates* it, and the runner + killswitch
*drain* it.

Ownership is carried by ``_current_runner`` — a ``ContextVar`` bound at the top
of each runner task body. ``asyncio.create_task`` copies the current context, so
every tool coroutine spawned inside a runner's task inherits the runner's name;
a subprocess spawned outside any runner (boot, external services) sees ``None``
and is deliberately NOT tracked here.

Reaping never signals a process group the daemon shares. A capability-*wrapped*
tool is spawned ``start_new_session=True`` (its own session) so ``killpg`` on its
pgid is safe; an *unwrapped* tool runs in the daemon's own session, so it is only
ever a single-pid ``kill`` — a group signal there would hit the daemon itself.
This is the ``runner_pgid != daemon_pgid`` guard relocated to the one real OS
boundary (the tool subprocess).
"""

from __future__ import annotations

import logging
import os
import signal
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("salient.daemon.proc_registry")

_current_runner: ContextVar[str | None] = ContextVar("salient_current_runner", default=None)

# Optional Tier-2 cgroup reaper (salient_core.daemon.cgroups.CgroupReaper),
# installed by the daemon at boot. When present, runner-owned tool subprocesses
# are ALSO placed into a per-runner cgroup at spawn so the killswitch can reap
# the whole subtree (fork+setsid+reparent-proof) via cgroup.kill. Duck-typed to
# avoid an import cycle; None (or unavailable) leaves the Tier-1 path unchanged.
_cgroup_reaper: Any | None = None


def set_cgroup_reaper(reaper: Any | None) -> None:
    global _cgroup_reaper
    _cgroup_reaper = reaper


def cgroup_reaper() -> Any | None:
    return _cgroup_reaper


@dataclass(frozen=True)
class ProcHandle:
    """A tracked tool subprocess. Only identity is stored — never the Process
    object — so the registry holds no references that would pin a finished child
    or leak across a runner restart."""

    pid: int
    wrapped: bool
    pgid: int
    argv0: str


# runner name -> {pid: ProcHandle}
_REGISTRY: dict[str, dict[int, ProcHandle]] = {}


def current_runner() -> str | None:
    """The runner name bound in the current context, or None outside a runner."""
    return _current_runner.get()


def bind_runner(name: str) -> Token[str | None]:
    """Bind the current context to ``name``; returns the reset token. Call once
    at the top of the runner task body so every tool coroutine inherits it.
    Also provisions the runner's Tier-2 cgroup when a reaper is installed."""
    if _cgroup_reaper is not None:
        try:
            _cgroup_reaper.ensure_runner(name)
        except Exception:  # noqa: BLE001 — cgroup provisioning never breaks bind
            pass
    return _current_runner.set(name)


def unbind_runner(token: Token[str | None]) -> None:
    with _suppress_ctx_error():
        _current_runner.reset(token)


class _suppress_ctx_error:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, *_: object) -> bool:
        # reset() raises ValueError if the token was created in a different
        # context (shouldn't happen — bind/unbind are same-task — but never let
        # teardown bookkeeping raise).
        return exc_type is ValueError


def register_subprocess(
    pid: int, *, wrapped: bool, argv0: str = "", runner: str | None = None
) -> None:
    """Record a freshly-spawned tool subprocess against its owning runner. A
    spawn with no bound runner (services, boot) is intentionally not tracked."""
    name = runner if runner is not None else _current_runner.get()
    if name is None:
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        pgid = pid
    _REGISTRY.setdefault(name, {})[pid] = ProcHandle(
        pid=pid, wrapped=wrapped, pgid=pgid, argv0=argv0
    )
    # Tier-2: also place the child in the runner's cgroup so a killswitch can
    # reap the whole subtree (fork+setsid-proof). Best-effort — the Tier-1
    # registry above is authoritative when cgroups are unavailable.
    if _cgroup_reaper is not None:
        try:
            _cgroup_reaper.place(name, pid)
        except Exception:  # noqa: BLE001
            pass


def deregister_subprocess(pid: int, runner: str | None = None) -> None:
    """Drop a subprocess that exited cleanly. Falls back to a full sweep if the
    keyed bucket misses (a tool may deregister from a slightly different
    context than it registered)."""
    name = runner if runner is not None else _current_runner.get()
    if name is not None and name in _REGISTRY and _REGISTRY[name].pop(pid, None) is not None:
        return
    for procs in _REGISTRY.values():
        if procs.pop(pid, None) is not None:
            return


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal — treat as alive (honest)
    return True


def registered(name: str) -> list[ProcHandle]:
    return list(_REGISTRY.get(name, {}).values())


def live_survivors(name: str) -> list[ProcHandle]:
    """Registered subprocesses of ``name`` whose pid is still alive."""
    return [h for h in _REGISTRY.get(name, {}).values() if _pid_alive(h.pid)]


def reap_runner(name: str, *, sig: int = signal.SIGKILL) -> list[int]:
    """Signal every still-live registered subprocess of ``name``. Foot-gun
    guard: ``killpg`` ONLY wrapped procs (own session); unwrapped are single-pid
    kills so a group signal never reaches the daemon's own group. Best-effort;
    returns the pids signalled. Idempotent — already-dead pids are skipped."""
    signalled: list[int] = []
    for handle in list(_REGISTRY.get(name, {}).values()):
        if not _pid_alive(handle.pid):
            continue
        try:
            if handle.wrapped:
                os.killpg(handle.pgid, sig)
            else:
                os.kill(handle.pid, sig)
            signalled.append(handle.pid)
        except (ProcessLookupError, PermissionError) as exc:
            _log.debug("reap %s pid=%s skipped: %s", name, handle.pid, exc)
    return signalled


def clear_runner(name: str) -> None:
    """Forget a runner's bucket (call after a runner task has exited)."""
    _REGISTRY.pop(name, None)


def known_pids() -> set[int]:
    """Every pid the registry believes is a runner-owned tool subprocess —
    used by the killswitch orphan audit to subtract accounted-for children."""
    return {pid for procs in _REGISTRY.values() for pid in procs}
