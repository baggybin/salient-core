"""Strong references for fire-and-forget background tasks.

The event loop keeps only a *weak* reference to a task, so a task whose
only strong reference is the return value of ``asyncio.create_task()`` can
be garbage-collected mid-flight — silently cancelling cleanup, shutdown,
or notification work before it finishes. The stdlib docs call this out
explicitly ("Save a reference to the result of this function, to avoid a
task disappearing mid-execution").

``spawn_background()`` parks each such task in a module-level set and
clears it on completion, so the task survives until it actually finishes
(or is cancelled). A module-level set is sufficient: salient runs one
daemon per process.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

_log = logging.getLogger("salient.daemon.tasks")

# Grace period, at teardown, for stragglers to honour cancellation before we
# give up and log that they refused.
_STRAGGLER_GRACE = 2.0

# Strong refs to in-flight fire-and-forget tasks. add_done_callback discards
# each on completion, so this never grows unbounded.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _on_background_done(task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.discard(task)
    # Retrieve the result so a failed fire-and-forget task doesn't log
    # "Task exception was never retrieved" and vanish silently — surface it.
    if not task.cancelled() and task.exception() is not None:
        _log.warning("background task %s failed: %r", task.get_name(), task.exception())


def spawn_background(
    coro: Coroutine[Any, Any, Any],
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    name: str | None = None,
) -> asyncio.Task:
    """Schedule ``coro`` as a background task and hold a strong reference to
    it until it completes, so it can't be GC'd mid-flight.

    Pass ``loop`` when scheduling from a context that already holds the loop
    explicitly (e.g. a sync callback that called ``get_running_loop()``);
    otherwise the running loop is used.
    """
    if loop is not None:
        task = loop.create_task(coro, name=name)
    else:
        task = asyncio.create_task(coro, name=name)
    return track_background(task)


def track_background(task: asyncio.Task) -> asyncio.Task:
    """Register an ALREADY-CREATED task in the background set so it survives to
    completion and is joined at daemon teardown (``join_background_tasks``).

    Use this — rather than ``spawn_background`` — when the caller needs the
    ``Task`` handle itself (e.g. to ``await asyncio.wait_for(shield(task), …)``
    with a bound) but still wants the fire-and-forget safety net and the
    teardown join. ``spawn_background`` is the create-and-park twin.
    """
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_background_done)
    return task


async def join_background_tasks(timeout: float = 10.0) -> None:
    """Await outstanding fire-and-forget tasks at daemon teardown, then cancel
    and reap any stragglers so none is dropped mid-flight (or logged as
    "Task was destroyed but it is pending").

    Loops snapshot-and-wait until the registry drains or ``timeout`` elapses:
    a task cancelled *by* shutdown can itself park a new background task (e.g.
    ``ask_agent``'s child-stop reap), so a single snapshot would miss it. After
    the deadline, remaining tasks are cancelled and gathered.

    MUST run BEFORE the daemon tears down agent backends: a child-stop task
    calls ``runner.cancel_job`` → ``backend.interrupt()``, which needs a live
    backend.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pending = [t for t in _BACKGROUND_TASKS if not t.done()]
        if not pending:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.wait(pending, timeout=remaining)
    stragglers = [t for t in _BACKGROUND_TASKS if not t.done()]
    for task in stragglers:
        task.cancel()
    if stragglers:
        # Bounded: a task that suppresses CancelledError must not hang teardown
        # forever. Surface any that refuse rather than await them indefinitely.
        _, still = await asyncio.wait(stragglers, timeout=_STRAGGLER_GRACE)
        if still:
            _log.error(
                "%d background task(s) refused cancellation at teardown: %s",
                len(still),
                ", ".join(sorted(t.get_name() for t in still)),
            )
