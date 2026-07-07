"""Unit tests for salient.daemon._event_hub.EventHub — the daemon-wide
global event tap behind the web console's /ws/events/all feed."""

import asyncio
import unittest

from salient_core.daemon._event_hub import (
    DAEMON_CAPABILITIES,
    EventHub,
    _EventObservationMixin,
)


class DaemonCapabilitiesShape(unittest.TestCase):
    """``DAEMON_CAPABILITIES`` is the single source of truth for the
    capability field surfaced on ``/api/whoami`` and the daemon-side
    ``_cmd_whoami`` RPC. Clients (web / desktop / TUI) read it to decide
    whether the daemon's ``replay`` flag is trustworthy; if this set
    regresses, a standalone newer bundle against an older daemon silently
    re-alerts the whole event ring on every reconnect."""

    def test_events_replay_flag_advertised(self):
        self.assertIn("events_replay_flag", DAEMON_CAPABILITIES)
        self.assertIs(DAEMON_CAPABILITIES["events_replay_flag"], True)

    def test_capability_values_are_bools(self):
        for key, value in DAEMON_CAPABILITIES.items():
            self.assertIsInstance(value, bool, f"{key!r} must be bool")


class EventHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_receives_published(self):
        hub = EventHub()
        q, snap = hub.subscribe()
        self.assertEqual(snap, [])
        hub.publish({"agent": "scanner", "kind": "text", "seq": 1})
        evt = await asyncio.wait_for(q.get(), timeout=1)
        self.assertEqual(evt["agent"], "scanner")

    async def test_fans_out_to_all_subscribers(self):
        hub = EventHub()
        q1, _ = hub.subscribe()
        q2, _ = hub.subscribe()
        hub.publish({"agent": "a", "seq": 1})
        self.assertEqual((await asyncio.wait_for(q1.get(), 1))["agent"], "a")
        self.assertEqual((await asyncio.wait_for(q2.get(), 1))["agent"], "a")

    async def test_snapshot_replays_ring(self):
        hub = EventHub()
        hub.publish({"agent": "a", "seq": 1})
        hub.publish({"agent": "b", "seq": 1})
        _q, snap = hub.subscribe()
        self.assertEqual([e["agent"] for e in snap], ["a", "b"])

    async def test_snapshot_frames_flagged_replay_originals_untouched(self):
        hub = EventHub()
        live = {"agent": "a", "kind": "refusal", "seq": 1}
        hub.publish(live)
        q, snap = hub.subscribe()
        # Snapshot frames carry replay=True so a client tells ring-buffer
        # backlog from live events deterministically (no wall-clock guessing).
        self.assertTrue(all(e.get("replay") is True for e in snap))
        # The flag lives on a COPY — the original event (and the ring) is
        # untouched, so the live delivery of a racing event isn't mislabeled.
        self.assertNotIn("replay", live)
        # A genuinely live publish after subscribe is delivered UNflagged.
        hub.publish({"agent": "a", "kind": "refusal", "seq": 2})
        evt = await asyncio.wait_for(q.get(), timeout=1)
        self.assertEqual(evt["seq"], 2)
        self.assertIsNone(evt.get("replay"))

    async def test_ring_is_bounded(self):
        hub = EventHub(ring_size=3)
        for i in range(5):
            hub.publish({"agent": "a", "seq": i})
        _q, snap = hub.subscribe()
        self.assertEqual([e["seq"] for e in snap], [2, 3, 4])

    async def test_unsubscribe_stops_delivery(self):
        hub = EventHub()
        q, _ = hub.subscribe()
        hub.unsubscribe(q)
        hub.publish({"agent": "a", "seq": 1})
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)

    async def test_backpressure_drops_when_full(self):
        # Queue maxsize is 2000; the 2001st publish must not raise or block
        # (a slow subscriber can't wedge an agent). Stays capped at 2000.
        hub = EventHub()
        q, _ = hub.subscribe()
        for i in range(2001):
            hub.publish({"agent": "a", "seq": i})
        self.assertEqual(q.qsize(), 2000)

    async def test_backpressure_drops_oldest_not_newest(self):
        # Drop-OLDEST: a never-drained queue keeps the most RECENT events so
        # a stalled tailer converges to live, not the stale front of the run.
        hub = EventHub(queue_size=3, evict_after=1000)
        q, _ = hub.subscribe()
        for i in range(5):
            hub.publish({"agent": "a", "seq": i})
        got = [q.get_nowait()["seq"] for _ in range(q.qsize())]
        self.assertEqual(got, [2, 3, 4])

    async def test_evicts_subscriber_that_never_drains(self):
        # A sub full AND not draining for `evict_after` consecutive publishes
        # is dropped, and a hub_evicted sentinel is left so an alive-but-slow
        # consumer can notice and resubscribe instead of hanging on get().
        # evict_min_seconds=0 exercises the count path deterministically.
        hub = EventHub(queue_size=2, evict_after=3, evict_min_seconds=0.0)
        q, _ = hub.subscribe()
        # Fill (2), then 3 more publishes with the queue full → eviction.
        for i in range(5):
            hub.publish({"agent": "a", "seq": i})
        self.assertEqual(len(hub._subs), 0)  # detached
        # The abandoned backlog is drained so the sentinel arrives FIRST.
        first = q.get_nowait()
        self.assertEqual(first["kind"], "hub_evicted")
        self.assertEqual(first["seq"], -1)
        self.assertIn("ts", first)
        # Post-eviction publishes no longer touch the queue.
        before = q.qsize()
        hub.publish({"agent": "a", "seq": 99})
        self.assertEqual(q.qsize(), before)

    async def test_synchronous_burst_does_not_evict_healthy_consumer(self):
        # The time floor: a tight synchronous publish burst (no yield to the
        # loop, so a healthy consumer never gets scheduled to drain) must NOT
        # evict, even though the full-streak sails past evict_after — wall
        # clock can't advance without yielding.
        hub = EventHub(queue_size=2, evict_after=3, evict_min_seconds=100.0)
        _q, _ = hub.subscribe()
        for i in range(50):
            hub.publish({"agent": "a", "seq": i})
        self.assertEqual(len(hub._subs), 1)  # survived the burst

    async def test_draining_resets_streak_and_prevents_eviction(self):
        # A consumer that keeps up (drains each event) never trips eviction,
        # no matter how many publishes — the full-streak resets on progress.
        hub = EventHub(queue_size=2, evict_after=3, evict_min_seconds=0.0)
        q, _ = hub.subscribe()
        for i in range(20):
            hub.publish({"agent": "a", "seq": i})
            q.get_nowait()  # stay caught up
        self.assertEqual(len(hub._subs), 1)  # still attached
        self.assertNotIn(q, hub._full_streak)  # trouble state cleared

    async def test_unsubscribe_clears_streak_state(self):
        # Unsubscribe must not leak per-sub bookkeeping.
        hub = EventHub(queue_size=2, evict_after=1000)
        q, _ = hub.subscribe()
        for i in range(4):  # push it into a full-streak
            hub.publish({"agent": "a", "seq": i})
        self.assertIn(q, hub._full_streak)
        self.assertIn(q, hub._full_since)
        hub.unsubscribe(q)
        self.assertNotIn(q, hub._full_streak)
        self.assertNotIn(q, hub._full_since)


class _EventsDaemon(_EventObservationMixin):
    """A daemon assembled from the kernel's shipped event-observation seam.
    Inherits ``subscribe_events``/``unsubscribe_events`` from
    ``_EventObservationMixin`` — the SAME code the assembled kernel daemon
    (via ``_QuestionsMixin``) runs — so these tests exercise the real
    delegation, not a hand-rolled copy. It only has to supply
    ``event_hub``."""

    def __init__(self) -> None:
        self.event_hub = EventHub()


class SubscribeEventsSeamTests(unittest.IsolatedAsyncioTestCase):
    """``DaemonServices.subscribe_events`` is the documented attach point
    for multi-client observation (web overlays, tailers, downstream
    socket/WebSocket relays). The contract observers rely on: bounded
    queue + replay snapshot on subscribe, drop-on-full so a slow or
    remote subscriber never stalls a producing agent."""

    def test_protocol_declares_the_seam(self):
        from salient_core.protocols import DaemonServices

        self.assertTrue(hasattr(DaemonServices, "subscribe_events"))
        self.assertTrue(hasattr(DaemonServices, "unsubscribe_events"))

    async def test_subscribe_delivers_live_and_replay(self):
        daemon = _EventsDaemon()
        daemon.event_hub.publish({"agent": "a", "seq": 1})
        q, snap = daemon.subscribe_events()
        self.assertEqual([e["seq"] for e in snap], [1])
        self.assertTrue(all(e.get("replay") is True for e in snap))
        daemon.event_hub.publish({"agent": "a", "seq": 2})
        evt = await asyncio.wait_for(q.get(), timeout=1)
        self.assertEqual(evt["seq"], 2)

    async def test_slow_subscriber_drops_instead_of_blocking(self):
        daemon = _EventsDaemon()
        q, _ = daemon.subscribe_events()
        # Never drained — the 2001st publish must drop silently, not raise
        # or block the (would-be) producing agent.
        for i in range(2001):
            daemon.event_hub.publish({"agent": "a", "seq": i})
        self.assertEqual(q.qsize(), 2000)

    async def test_unsubscribe_detaches_the_client(self):
        daemon = _EventsDaemon()
        q, _ = daemon.subscribe_events()
        daemon.unsubscribe_events(q)
        daemon.event_hub.publish({"agent": "a", "seq": 1})
        self.assertEqual(q.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
