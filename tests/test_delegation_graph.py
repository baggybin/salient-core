"""Tests for salient.delegation_graph — the cycle-detection helpers.

These pin the topology semantics that bus.py's ask_agent relies on
to refuse the closing edge of a deadlock cycle. The 3-way deadlock
trace from BUS_DESIGN_AUDIT.md is preserved here as the canonical
positive case.

Pure helpers — no daemon, no asyncio. The ``_Call`` namedtuple
satisfies the duck-typed protocol the graph functions expect.
"""

from __future__ import annotations

import unittest
from collections import namedtuple

from salient_core.coord.delegation_graph import (
    all_cycles,
    build_adjacency,
    find_cycle,
    format_cycle,
)

# Lightweight stand-in for BusCall — only .caller + .target are read.
Call = namedtuple("Call", ["caller", "target"])


class FindCycleTests(unittest.TestCase):
    """``find_cycle`` answers 'would adding caller→target close a
    cycle in the current graph?' Each test enumerates the active
    in-flight edges, then proposes a new edge."""

    def test_empty_graph_no_cycle(self):
        self.assertIsNone(find_cycle([], "a", "b"))

    def test_two_node_back_edge_is_a_cycle(self):
        # A → B is in flight. Now B wants to call A. That closes.
        calls = [Call("a", "b")]
        cycle = find_cycle(calls, caller="b", target="a")
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle, ["a", "b", "a"])

    def test_three_way_deadlock_canonical_case(self):
        # The exact scenario from 2026-05-18 (BUS_DESIGN_AUDIT §"Tracing"):
        #   playstation → deepseek_osint              (already in flight)
        #   deepseek_osint → deepseek_websearch       (already in flight)
        #   deepseek_websearch → playstation          (PROPOSED — closes it)
        calls = [
            Call("playstation", "deepseek_osint"),
            Call("deepseek_osint", "deepseek_websearch"),
        ]
        cycle = find_cycle(calls, caller="deepseek_websearch", target="playstation")
        self.assertIsNotNone(cycle)
        # path should walk forward from the target, through the graph,
        # back to the proposed caller, then close the loop
        self.assertEqual(cycle[0], "playstation")  # start at proposed target
        self.assertEqual(cycle[-1], "playstation")  # closes back to it
        self.assertIn("deepseek_osint", cycle)
        self.assertIn("deepseek_websearch", cycle)

    def test_unrelated_calls_no_false_positive(self):
        # Edges exist but neither caller nor target is part of an
        # active chain.
        calls = [
            Call("alice", "bob"),
            Call("charlie", "dave"),
        ]
        self.assertIsNone(find_cycle(calls, caller="eve", target="frank"))
        self.assertIsNone(find_cycle(calls, caller="alice", target="frank"))

    def test_long_chain_with_back_edge(self):
        # 10-deep chain, then propose closing edge from the tail back
        # to the head — must detect.
        chain = [Call(f"a{i}", f"a{i + 1}") for i in range(10)]
        cycle = find_cycle(chain, caller="a10", target="a0")
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], "a0")
        self.assertEqual(cycle[-1], "a0")
        self.assertEqual(len(cycle), 12)  # 11 unique nodes + closing repeat

    def test_self_edge_treated_as_cycle(self):
        # Defence in depth — bus.py already refuses "ask yourself" but
        # the graph helper should also catch it.
        cycle = find_cycle([], caller="a", target="a")
        self.assertEqual(cycle, ["a", "a"])

    def test_max_depth_safety_returns_none(self):
        # Pathological deep chain — the helper bails to None rather
        # than hanging or recursing forever.
        chain = [Call(f"a{i}", f"a{i + 1}") for i in range(200)]
        result = find_cycle(chain, caller="a999", target="a0", max_depth=64)
        # No cycle to a999 exists in the chain, so result is None
        # either way — but the point is it returns within bound.
        self.assertIsNone(result)

    def test_disconnected_components_no_false_positive(self):
        # Two unrelated cycles in different parts of the graph; adding
        # an edge in component A shouldn't detect the cycle in B.
        calls = [
            Call("a", "b"),
            Call("b", "c"),
            Call("x", "y"),
            Call("y", "x"),
        ]
        # Propose a NEW edge in component A that doesn't close — should
        # be fine, not influenced by component B's cycle.
        self.assertIsNone(find_cycle(calls, caller="c", target="d"))


class AllCyclesTests(unittest.TestCase):
    """``all_cycles`` enumerates every cycle in the CURRENT graph
    (no proposed edge). Used by the operator-facing tree renderer."""

    def test_empty_graph(self):
        self.assertEqual(all_cycles([]), [])

    def test_acyclic_chain(self):
        chain = [Call("a", "b"), Call("b", "c"), Call("c", "d")]
        self.assertEqual(all_cycles(chain), [])

    def test_simple_two_node_cycle(self):
        calls = [Call("a", "b"), Call("b", "a")]
        cycles = all_cycles(calls)
        self.assertEqual(len(cycles), 1)
        self.assertIn("a", cycles[0])
        self.assertIn("b", cycles[0])

    def test_three_way_deadlock_after_close(self):
        # Same canonical case, but this time the closing edge is
        # ALREADY in the graph — caller wants to see it on a CLI.
        calls = [
            Call("playstation", "deepseek_osint"),
            Call("deepseek_osint", "deepseek_websearch"),
            Call("deepseek_websearch", "playstation"),
        ]
        cycles = all_cycles(calls)
        self.assertEqual(len(cycles), 1)
        nodes_in_cycle = set(cycles[0])
        self.assertIn("playstation", nodes_in_cycle)
        self.assertIn("deepseek_osint", nodes_in_cycle)
        self.assertIn("deepseek_websearch", nodes_in_cycle)

    def test_two_separate_cycles(self):
        calls = [
            Call("a", "b"),
            Call("b", "a"),
            Call("x", "y"),
            Call("y", "z"),
            Call("z", "x"),
        ]
        cycles = all_cycles(calls)
        self.assertEqual(len(cycles), 2)


class BuildAdjacencyTests(unittest.TestCase):
    def test_groups_by_caller(self):
        calls = [
            Call("a", "b"),
            Call("a", "c"),
            Call("d", "e"),
        ]
        adj = build_adjacency(calls)
        self.assertEqual(set(adj["a"]), {"b", "c"})
        self.assertEqual(adj["d"], ["e"])
        self.assertNotIn("b", adj)  # b has no outgoing


class FormatCycleTests(unittest.TestCase):
    def test_three_node_format(self):
        path = ["playstation", "deepseek_osint", "deepseek_websearch", "playstation"]
        formatted = format_cycle(path)
        self.assertEqual(
            formatted,
            "playstation → deepseek_osint → deepseek_websearch → playstation",
        )


if __name__ == "__main__":
    unittest.main()
