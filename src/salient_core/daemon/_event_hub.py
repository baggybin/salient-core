"""Daemon-wide live event hub.

Every ``AgentRunner._publish`` feeds its event here in addition to the
runner's own per-agent subscribers, giving ONE global stream that the web
console's "all agents" overlay (``/ws/events/all``) taps — so the operator
sees every agent's thinking / tool-calls / text, not just the agents whose
panes happen to be open.

Mirrors the ``QuestionInbox`` subscribe/unsubscribe shape. Backpressure
diverges from the runner's per-agent tail on purpose: where the runner
drops the NEWEST event on a full queue, the hub drops the OLDEST
(``publish``) so a stalled tailer converges back to live instead of
replaying a 2000-event backlog forever — and a subscriber that makes zero
progress for ``evict_after`` consecutive publishes is dropped entirely
(with a ``hub_evicted`` sentinel), a hub-side backstop against an observer
that leaks its subscription on an unclean disconnect. Either way a slow
subscriber never blocks the agent that produced the event.

Events are FROZEN once published: the producer deep-copies ``meta`` at
birth (``runner._publish``), but the top-level event dict is still shared
by reference across ``recent_events``, the ring, and every subscriber
queue. A consumer that needs to annotate a frame must copy it first (the
``subscribe`` replay path models this with ``{**evt, "replay": True}``);
mutating a frame in place corrupts it for every other observer.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections import deque
from contextlib import suppress
from typing import Any


def fork_event(evt: dict[str, Any]) -> dict[str, Any]:
    """Return an isolated copy of ``evt`` for handoff to ONE consumer.

    The producer's rings (``recent_events``, the hub ring) keep the canonical
    birth object, which nobody mutates after publish. Every object handed to a
    consumer — a subscriber ``put_nowait`` or a replay snapshot frame — is a
    fresh fork instead, so one subscriber annotating ``evt["meta"][...]`` can't
    corrupt another subscriber or a later replay.

    Only ``meta`` is deep-copied: every other top-level field is an immutable
    scalar/string, so a shallow ``dict(evt)`` isolates them. Stays plain-dict
    all the way down (no proxies/tuples) so the web console can still
    ``json.dumps`` the frame. Cheap in the common case — token-delta events
    usually carry no ``meta``, degrading this to a single shallow copy."""
    c = dict(evt)
    m = c.get("meta")
    if m is not None:
        c["meta"] = copy.deepcopy(m)
    return c


# Capabilities the daemon advertises on the ``whoami`` surface so a client can
# detect skew (e.g. a standalone desktop bundle running ahead of the older
# daemon it talks to). Clients that see a missing or False cap should degrade
# gracefully — for ``events_replay_flag`` the fail-safe is "don't bump unread
# counts on the blocks indicator" (the badge stays suppressed under the old
# daemon, avoiding a re-alert storm on every reconnect).
DAEMON_CAPABILITIES: dict[str, bool] = {"events_replay_flag": True}


class EventHub:
    """Fan-out of every agent's published events to N subscribers, plus a
    bounded ring so a late subscriber replays recent backlog on connect."""

    def __init__(
        self,
        ring_size: int = 1000,
        queue_size: int = 2000,
        evict_after: int = 1000,
        evict_min_seconds: float = 30.0,
    ) -> None:
        self._subs: list[asyncio.Queue] = []
        self._ring: deque = deque(maxlen=max(1, ring_size))
        self._queue_size = max(1, queue_size)
        # Per-sub count of CONSECUTIVE publishes where the queue was full at
        # put time — i.e. the consumer drained NOTHING since the last publish.
        # A key is present ONLY while that sub is in trouble (popped on any
        # progress, unsubscribe, or eviction), so "in the dict" == "starving".
        # This is the eviction signal because drop-oldest means ``put_nowait``
        # never raises ``QueueFull`` to key off.
        self._full_streak: dict[asyncio.Queue, int] = {}
        # monotonic() when each starving sub's streak began. Paired with
        # ``evict_min_seconds`` so a tight SYNCHRONOUS publish burst (many
        # events with no yield to the loop) can't evict a healthy consumer
        # that simply never got scheduled to drain — eviction needs both the
        # count AND real elapsed time, which a non-yielding burst can't run up.
        # Default floor is generous (30s): the real "consumer" is a server-side
        # WS handler that legitimately blocks on ``await ws.send()`` under TCP
        # backpressure when a browser tab is backgrounded — a false eviction
        # there can strand the overlay (its resubscribe path is out-of-repo).
        self._full_since: dict[asyncio.Queue, float] = {}
        self._evict_after = max(1, evict_after)
        self._evict_min_seconds = max(0.0, evict_min_seconds)
        # Monotonic-descending seq for hub-emitted sentinels (hub_evicted), so
        # two evictions on one long-lived consumer connection don't collide on
        # the dedupe key and suppress the second notice. Negative == hub-origin.
        self._sentinel_seq = 0

    def subscribe(self) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        """Return a live queue + a snapshot of recent events.

        Order matters (same as ``AgentRunner.subscribe``): append the queue
        first, then read the ring. Any event racing between the two is
        captured by both paths; the consumer dedupes on ``(agent, epoch,
        seq)``. The ``epoch`` is REQUIRED in the key: ``seq`` resets when a
        same-name runner is rebuilt, so ``(agent, seq)`` alone would let a
        replayed event from the old incarnation suppress a live event with a
        colliding seq from the new one.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subs.append(q)
        # Flag snapshot frames `replay` so a subscriber tells ring-buffer
        # backlog from genuinely live events DETERMINISTICALLY — no wall-clock
        # "bootstrap window" guessing on the client (which mis-counts a slow
        # replay as live, or swallows a block that lands right after connect).
        # Fork each ring frame: the ring keeps the canonical unmarked event, so
        # other consumers and the live delivery of an event that races between
        # append-queue and read-ring are unaffected — and this replay consumer
        # gets a deeply-isolated copy it can annotate without corrupting the
        # ring or another subscriber.
        snapshot = [{**fork_event(evt), "replay": True} for evt in self._ring]
        return q, snapshot

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with suppress(ValueError):
            self._subs.remove(q)
        self._full_streak.pop(q, None)
        self._full_since.pop(q, None)

    def publish(self, evt: dict[str, Any]) -> None:
        # Always record to the ring so a tailer that connects later can
        # replay the backlog even if nobody was subscribed at the time.
        self._ring.append(evt)
        for q in list(self._subs):
            # Drop-OLDEST on backpressure so a stalled tailer converges back
            # to live instead of forever replaying a stale backlog. The
            # get_nowait/put_nowait pair is atomic on the single event loop —
            # there is NO `await` between them — so the second put_nowait can
            # never fail and no consumer can observe the transiently-freed
            # slot. This whole method's correctness (that invariant, plus the
            # streak/`_full_since` bookkeeping being race-free) hinges on
            # publish() staying synchronous. DO NOT add an `await` here.
            if q.full():
                # Full at put time == drained nothing since the last publish.
                # Advance the streak (the eviction signal) and make room.
                streak = self._full_streak.get(q, 0) + 1
                self._full_streak[q] = streak
                self._full_since.setdefault(q, time.monotonic())
                with suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            else:
                # Any progress clears the trouble state.
                self._full_streak.pop(q, None)
                self._full_since.pop(q, None)
                streak = 0
            # Fork per subscriber so one tailer can't corrupt another's frame
            # (the ring above keeps the canonical object).
            q.put_nowait(fork_event(evt))
            # Evict a consumer that has made ZERO progress for `evict_after`
            # consecutive publishes AND for at least `evict_min_seconds` of
            # real time: functionally dead even if its socket is technically
            # open. Belt-and-suspenders against an observer (notably the
            # out-of-repo /ws/events/all handler) that leaks its subscription
            # on an unclean disconnect — without this, `_subs` grows
            # monotonically across reconnect churn.
            if streak >= self._evict_after and self._starved_long_enough(q):
                self._evict(q)

    def _starved_long_enough(self, q: asyncio.Queue) -> bool:
        # time.monotonic() directly (not loop.time()) so this needs no running
        # loop and has no error branch to swallow — same monotonic clock, one
        # consistent reference for both the streak start and this check.
        if self._evict_min_seconds <= 0:
            return True
        started = self._full_since.get(q)
        if started is None:
            return False
        return time.monotonic() - started >= self._evict_min_seconds

    def _evict(self, q: asyncio.Queue) -> None:
        """Drop a dead subscriber, leaving a ``hub_evicted`` sentinel so a
        wrongly-evicted-but-alive consumer SEES the eviction (and can
        resubscribe for a fresh ring replay) rather than hanging on
        ``q.get()`` forever. Idempotent — a later ``finally`` cleanup on an
        already-evicted sub is harmless. Safe to call from any site, not just
        publish()'s just-put-a-full-queue path."""
        with suppress(ValueError):
            self._subs.remove(q)
        self._full_streak.pop(q, None)
        self._full_since.pop(q, None)
        # Drain the abandoned backlog so the sentinel is delivered FIRST (not
        # behind a full queue of stale events) and the references release now.
        # Bounded by queue_size (≤2000 default) synchronous get_nowaits on the
        # producer's publish call — cheap at that size; revisit if queue_size
        # is ever raised by orders of magnitude.
        while not q.empty():
            q.get_nowait()
        # Independent from the drain: even if the queue was already empty, the
        # sentinel must still land. Schema-complete (ts/seq/epoch-shaped) so a
        # consumer that indexes those fields unconditionally can't choke on it.
        # A unique negative seq (hub-origin) so repeated evictions on one
        # consumer connection don't dedupe-collide and swallow the notice.
        self._sentinel_seq -= 1
        with suppress(asyncio.QueueFull):
            q.put_nowait(
                {
                    "agent": "_hub",
                    "kind": "hub_evicted",
                    "epoch": 0,
                    "seq": self._sentinel_seq,
                    "ts": time.time(),
                    "text": "subscriber evicted: fell too far behind",
                }
            )


class _EventObservationMixin:
    """The concrete ``DaemonServices.subscribe_events`` /
    ``unsubscribe_events`` seam — a thin delegation to the daemon's own
    ``EventHub``. Composed into a daemon (the kernel mixes it into
    ``_QuestionsMixin`` so every assembled daemon inherits it; a standalone
    daemon like the tutor implements the same two methods directly).

    Every observer — the shoulder-surf driver, a web overlay, a downstream
    socket/WebSocket relay — attaches HERE rather than reaching for
    ``daemon.event_hub`` directly, so the attach point stays a single
    swappable method instead of a hard reference to the hub object. Expects
    ``self.event_hub`` to be an ``EventHub`` (or any object with the same
    ``subscribe``/``unsubscribe`` shape)."""

    event_hub: EventHub

    def subscribe_events(self) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        return self.event_hub.subscribe()

    def unsubscribe_events(self, q: asyncio.Queue) -> None:
        self.event_hub.unsubscribe(q)
