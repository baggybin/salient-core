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
from collections.abc import Coroutine
from typing import Any

# Strong refs to in-flight fire-and-forget tasks. add_done_callback discards
# each on completion, so this never grows unbounded.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


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
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
