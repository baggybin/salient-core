"""Structured meta dict for known bus tool-call events.

Pre-fix: tool-call events for bus calls rendered as dense one-liners like
  `tool-call: bus.red_lead.ask_agent  name=scanner prompt="..." max_turns=5`
which buried the source/target/intent under string formatting.

Fix: the runner extracts a structured meta dict for known bus calls
and attaches it to the published event. The web pane renders a rich
card (source-chip → fn-badge → target-chip + collapsible body); CLI
falls through to the existing text label.

This file pins the meta-extraction contract — the web-side rendering
is exercised by hand against a live daemon.
"""

from __future__ import annotations

import unittest


def _make_runner(name: str = "red_lead"):
    from salient_core.daemon import AgentRunner

    return AgentRunner(
        name=name,
        cfg={},
        prompt_timeout=60.0,
        idle_timeout=0.0,
    )


class BusToolMetaExtractionTests(unittest.TestCase):
    """`_bus_tool_meta(tool_name, input)` returns a structured dict for
    bus calls and None for everything else."""

    def test_ask_agent_extracts_source_target_prompt(self):
        r = _make_runner("red_lead")
        meta = r._bus_tool_meta(
            "mcp__bus__red_lead__ask_agent",
            {
                "name": "scanner",
                "prompt": "Fresh recon scan against 10.13.38.57…",
                "max_turns": 5,
                "deliverable": "Per-port lines …",
            },
        )
        self.assertEqual(meta["subkind"], "bus_ask_agent")
        self.assertEqual(
            meta["source"],
            "red_lead",
            "Source must be the runner's canonical name, NOT parsed "
            "from the wire-alias in tool_name (offensive agent names "
            "get aliased before SDK registration; the runner already "
            "holds the real name).",
        )
        self.assertEqual(meta["target"], "scanner")
        self.assertEqual(meta["prompt"], "Fresh recon scan against 10.13.38.57…")
        self.assertEqual(meta["max_turns"], 5)
        self.assertEqual(meta["deliverable"], "Per-port lines …")
        self.assertEqual(
            meta["prefer_primary"],
            False,
            "prefer_primary defaults to False when not set — the web "
            "card's substitute-bypass chip should NOT light up unless "
            "the caller explicitly requested the primary.",
        )

    def test_ask_agent_carries_prefer_primary_when_set(self):
        r = _make_runner("red_lead")
        meta = r._bus_tool_meta(
            "mcp__bus__red_lead__ask_agent",
            {"name": "msf", "prompt": "x", "prefer_primary": True},
        )
        self.assertTrue(meta["prefer_primary"])

    def test_ask_partner_subkind(self):
        """Shadow → primary calls. Same shape as ask_agent without
        prefer_primary (substitute routing is irrelevant for the
        explicit-partner flow)."""
        r = _make_runner("deepseek_msf")
        meta = r._bus_tool_meta(
            "mcp__bus__deepseek_msf__ask_partner",
            {"name": "msf", "prompt": "second opinion on this module"},
        )
        self.assertEqual(meta["subkind"], "bus_ask_partner")
        self.assertEqual(meta["source"], "deepseek_msf")
        self.assertEqual(meta["target"], "msf")
        self.assertNotIn(
            "prefer_primary",
            meta,
            "ask_partner is shadow→primary by definition; the field "
            "would never be set, so omitting it keeps the meta tight.",
        )

    def test_ask_operator_target_is_operator(self):
        r = _make_runner("manager")
        meta = r._bus_tool_meta(
            "mcp__bus__manager__ask_operator",
            {"question": "should we proceed against host X?"},
        )
        self.assertEqual(meta["subkind"], "bus_ask_operator")
        self.assertEqual(meta["source"], "manager")
        self.assertEqual(
            meta["target"],
            "operator",
            "ask_operator's target chip must read 'operator' so the "
            "card visually distinguishes operator-bound questions "
            "from peer delegations at a glance.",
        )
        self.assertEqual(meta["question"], "should we proceed against host X?")

    def test_context_write_extracts_key_and_value(self):
        r = _make_runner("scanner")
        meta = r._bus_tool_meta(
            "mcp__bus__scanner__context_write",
            {"key": "offshore-001/recon", "value": "host: 10.0.0.5\n..."},
        )
        self.assertEqual(meta["subkind"], "bus_context_write")
        self.assertEqual(meta["source"], "scanner")
        self.assertEqual(meta["key"], "offshore-001/recon")
        self.assertIn("host: 10.0.0.5", meta["value"])

    def test_non_bus_tool_returns_none(self):
        r = _make_runner()
        for tool in (
            "mcp__scanner__nmap_scan",  # factory tool, not bus
            "mcp__manager__ask_agent",  # not bus (no __bus__ prefix)
            "nmap_scan",  # bare, no mcp__ prefix
            "",  # empty
        ):
            with self.subTest(tool=tool):
                self.assertIsNone(
                    r._bus_tool_meta(tool, {}),
                    f"_bus_tool_meta({tool!r}) must return None so the "
                    "renderer falls through to the standard text "
                    "label. Returning a partial meta would mis-render "
                    "non-bus tool calls as bus cards.",
                )

    def test_unknown_bus_function_returns_none(self):
        """A bus tool we don't special-case (e.g. kg_assert) should
        return None — the card has no shape for these and we don't
        want a generic catch-all that might render confusingly."""
        r = _make_runner()
        meta = r._bus_tool_meta(
            "mcp__bus__manager__kg_assert",
            {"subject": "x", "predicate": "y", "object": "z"},
        )
        self.assertIsNone(meta)

    def test_handles_missing_input_dict(self):
        """Defensive: tool_input may be None (SDK can emit without an
        input on rare error paths). Must not crash."""
        r = _make_runner()
        meta = r._bus_tool_meta(
            "mcp__bus__red_lead__ask_agent",
            None,
        )
        self.assertEqual(meta["subkind"], "bus_ask_agent")
        self.assertIsNone(meta["target"])
        self.assertIsNone(meta["prompt"])


class PublishCarriesMetaTests(unittest.TestCase):
    """`_publish` accepts an optional `meta` kwarg and attaches it to
    the streamed event. Without this the web client never sees the
    structured fields the card renderer expects."""

    def test_publish_adds_meta_when_supplied(self):
        r = _make_runner()
        r._publish(
            "tool-call",
            "rendered text",
            meta={"subkind": "bus_ask_agent", "source": "x", "target": "y"},
        )
        evts = list(r.recent_events)
        self.assertEqual(len(evts), 1)
        self.assertIn(
            "meta",
            evts[0],
            "Published event must carry the meta dict so the web "
            "client can detect known subkinds and render the card.",
        )
        self.assertEqual(evts[0]["meta"]["subkind"], "bus_ask_agent")

    def test_publish_omits_meta_field_when_not_supplied(self):
        """Events without structured meta must NOT include a `meta`
        key — keeps the event payload minimal for the common case
        and makes the client's `evt.meta?.subkind` check fall
        through cleanly to the existing text path."""
        r = _make_runner()
        r._publish("text", "just text")
        evts = list(r.recent_events)
        self.assertNotIn("meta", evts[0])

    def test_publish_omits_meta_when_supplied_falsy(self):
        """`meta=None` or `meta={}` is treated as no-meta. Tool-call
        sites build the dict conditionally on bus-call detection; an
        empty dict from a non-bus call must not become a stray field."""
        r = _make_runner()
        r._publish("tool-call", "x", meta=None)
        r._publish("tool-call", "y", meta={})
        evts = list(r.recent_events)
        for e in evts:
            self.assertNotIn("meta", e)


class PublishEpochTests(unittest.TestCase):
    """Every published event carries `_epoch` — the per-incarnation stamp
    that lets a hub-ring replay dedupe on `(agent, epoch, seq)` instead of
    `(agent, seq)`. Without it, a same-name runner rebuilt after teardown
    reuses `seq` from 0, and an old ring event silently suppresses a live
    event with a colliding seq from the new incarnation."""

    def test_publish_stamps_epoch(self):
        r = _make_runner()
        r._epoch = 7
        r._publish("text", "hello")
        evt = list(r.recent_events)[0]
        self.assertEqual(evt["epoch"], 7)

    def test_epoch_defaults_to_zero_when_unstamped(self):
        # A runner the factory never stamped (bare construction) still
        # publishes a well-formed event — epoch just falls back to 0.
        r = _make_runner()
        r._publish("text", "hello")
        self.assertEqual(list(r.recent_events)[0]["epoch"], 0)

    def test_seq_still_advances_within_an_epoch(self):
        r = _make_runner()
        r._epoch = 3
        r._publish("text", "a")
        r._publish("text", "b")
        evts = list(r.recent_events)
        self.assertEqual([(e["epoch"], e["seq"]) for e in evts], [(3, 1), (3, 2)])


class PublishMetaIsFrozenTests(unittest.TestCase):
    """`_publish` deep-copies `meta` at birth. The one event dict is aliased
    into three fan-out targets (recent_events, the hub ring, every subscriber
    queue) that share the nested `meta`; a producer that reuses or mutates its
    `meta` dict afterwards must NOT retroactively rewrite already-recorded
    history."""

    def test_meta_is_deep_copied_from_producer(self):
        r = _make_runner()
        source = {"subkind": "bus_ask_agent", "nested": {"prompt": "original"}}
        r._publish("tool-call", "rendered", meta=source)
        # Mutate the producer's dict AFTER publish — including the nested dict.
        source["subkind"] = "MUTATED"
        source["nested"]["prompt"] = "MUTATED"
        recorded = list(r.recent_events)[0]["meta"]
        self.assertEqual(recorded["subkind"], "bus_ask_agent")
        self.assertEqual(recorded["nested"]["prompt"], "original")

    def test_non_copyable_meta_degrades_instead_of_crashing(self):
        # _publish is on the agent's hot path and must never raise. A meta
        # value that can't be deep-copied (here: a threading.Lock) falls back
        # to a shallow copy — the event is still published, top-level alias
        # still severed — rather than taking down the producing agent.
        import threading

        r = _make_runner()
        lock = threading.Lock()
        r._publish("tool-call", "rendered", meta={"subkind": "x", "lock": lock})
        evt = list(r.recent_events)[0]
        self.assertEqual(evt["meta"]["subkind"], "x")
        self.assertIs(evt["meta"]["lock"], lock)  # shallow: same object, fine


if __name__ == "__main__":
    unittest.main()
