"""The delegation echo must forward the child's ``text_full``.

A delegated child truncates a long body (e.g. `thinking`) to a display string
ending `... [+N chars]` and publishes the FULL body alongside as ``text_full``,
which the web pane uses to make the marker a clickable expand control. The echo
that mirrors a child's events onto the caller's pane previously re-published only
the truncated ``text`` and dropped ``text_full`` — so on the caller's pane the
`... [+N chars]` marker on a `delegated:thinking` row was inert. Pin the forward.
"""

from __future__ import annotations

import asyncio
import contextlib
import unittest

from salient_core.bus import _delegation as D


class _Recorder:
    """A caller_runner stand-in that records ``_publish`` calls with the same
    signature the real runner exposes (``text_full`` keyword-only)."""

    def __init__(self) -> None:
        self.name = "manager"
        self.calls: list[dict] = []

    def _publish(self, kind, text, *, text_full=None, meta=None):
        self.calls.append({"kind": kind, "text": text, "text_full": text_full, "meta": meta})


class DelegationEchoTextFullTests(unittest.IsolatedAsyncioTestCase):
    async def _echo_one(self, evt: dict) -> _Recorder:
        caller = _Recorder()
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(evt)
        task = asyncio.create_task(
            D._echo_child_stream(q, caller, child="deepseek_red_lead", child_job_id=7)
        )
        # Let the loop drain the single event, then cancel (it loops forever).
        for _ in range(100):
            if caller.calls:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return caller

    async def test_echo_forwards_text_full(self):
        caller = await self._echo_one(
            {
                "job_id": 7,
                "kind": "thinking",
                "text": "truncated head... [+4805 chars]",
                "text_full": "the full multi-line thinking body",
            }
        )
        self.assertEqual(len(caller.calls), 1)
        call = caller.calls[0]
        self.assertEqual(call["kind"], "delegated:thinking")
        # THE PIN: the child's full body rides along so the caller-pane expand
        # toggle can show it. Pre-fix this was None and the marker was inert.
        self.assertEqual(call["text_full"], "the full multi-line thinking body")

    async def test_echo_without_text_full_forwards_none(self):
        # A short / untruncated child event carries no text_full — the echo
        # forwards None, never fabricates one.
        caller = await self._echo_one({"job_id": 7, "kind": "reply", "text": "short reply"})
        self.assertEqual(len(caller.calls), 1)
        self.assertIsNone(caller.calls[0]["text_full"])


if __name__ == "__main__":
    unittest.main()
