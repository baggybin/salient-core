"""Topology-aware analysis of the in-flight delegation graph.

The bus's ``_bus_calls`` dict tracks every active ``ask_agent`` call
as ``(caller, target)`` edges. Each ``ask_agent`` checks edges in
isolation and never looks at the graph as a whole, so a closing
back-edge (A → B → C → A) registers cleanly and produces a deadlock
that only the 60-minute reaper notices.

This module gives us graph-aware helpers:

- ``find_cycle(calls, caller, target)`` — would adding the edge
  ``caller → target`` close a cycle? Returns the cycle path
  ``[target, ..., caller]`` if yes, ``None`` if not.
- ``all_cycles(calls)`` — list every cycle in the CURRENT graph
  (no proposed edge). Used by the operator-facing tree renderer
  to mark stuck loops in red.
- ``build_adjacency(calls)`` — ``{caller: [target, ...]}`` for any
  caller that has outgoing in-flight calls.

The "call" objects are duck-typed: anything with ``.caller`` and
``.target`` attributes works. We use this so the helpers can be
exercised against ``BusCall`` instances at runtime AND against tiny
fake-call objects in tests without dragging in the daemon.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable
from typing import Protocol

_log = logging.getLogger("salient.delegation_graph")


class _CallLike(Protocol):
    caller: str
    target: str


def build_adjacency(calls: Iterable[_CallLike]) -> dict[str, list[str]]:
    """Return ``{caller: [target, ...]}`` for all calls.

    Any state — awaiting_agent_start / awaiting_delegation_gate /
    awaiting_reply — counts as an active edge. We deliberately do
    NOT filter by state: an unresolved call in any state ties up
    the caller's coroutine and counts toward cycles.
    """
    adj: dict[str, list[str]] = {}
    for call in calls:
        adj.setdefault(call.caller, []).append(call.target)
    return adj


def find_cycle(
    calls: Iterable[_CallLike],
    caller: str,
    target: str,
    max_depth: int = 64,
) -> list[str] | None:
    """Does adding the proposed edge ``caller → target`` close a
    cycle in the in-flight delegation graph?

    Walk forward from ``target`` via existing outgoing edges. If
    we reach ``caller``, the proposed edge would close a cycle;
    return the path ``[target, ..., caller]`` (so the caller can
    print ``caller → target → ... → caller``).

    Returns ``None`` when no cycle is detected. ``max_depth`` is
    a safety bound — we don't walk arbitrarily deep trees.
    """
    if caller == target:
        # Self-loops are already caught by ask_agent's "cannot ask
        # yourself" guard. Return early for defence in depth.
        return [target, caller]

    adj = build_adjacency(calls)

    # BFS from target, remembering each node's predecessor so we
    # can reconstruct the path when we find caller.
    parents: dict[str, str] = {}
    queue: deque[tuple[str, int]] = deque([(target, 0)])
    visited: set[str] = {target}
    while queue:
        node, depth = queue.popleft()
        if depth > max_depth:
            # A graph deep enough to trip the bound is itself an anomaly the
            # operator should see — don't bail silently. Treat as no-cycle to
            # avoid a false refusal, but surface the runaway.
            _log.warning(
                "find_cycle bailed at max_depth=%d walking %s → %s; treating "
                "as no-cycle (delegation graph unusually deep — possible "
                "runaway delegation)",
                max_depth,
                caller,
                target,
            )
            return None
        for nxt in adj.get(node, ()):
            if nxt == caller:
                # Reconstruct path: target → ... → node → caller
                path = [caller, node]
                cur = node
                while cur in parents:
                    cur = parents[cur]
                    path.append(cur)
                # path now reads caller, node, ..., target — reverse
                path.reverse()
                # Append the closing edge so the operator sees the
                # full loop: target → ... → caller → target  (the
                # cycle reads start-to-start)
                return path + [target]
            if nxt not in visited:
                visited.add(nxt)
                parents[nxt] = node
                queue.append((nxt, depth + 1))
    return None


def all_cycles(calls: Iterable[_CallLike]) -> list[list[str]]:
    """Find every cycle in the active graph (no proposed edge).

    Uses DFS with a recursion stack to detect back-edges. Each
    cycle is returned once in canonical form (rotated so the
    lexically smallest node leads). Deduplicated.

    For the operator-facing tree CLI: mark every edge that's part
    of any returned cycle as ``⚠ CYCLE``.
    """
    calls_list = list(calls)
    adj = build_adjacency(calls_list)
    seen: set[tuple[str, ...]] = set()
    out: list[list[str]] = []

    def canonical(path: list[str]) -> tuple[str, ...]:
        """Rotate so the lex-smallest node is first; canonicalises
        any rotation of the same cycle to one key."""
        if not path:
            return ()
        i = min(range(len(path)), key=lambda k: path[k])
        return tuple(path[i:] + path[:i])

    def dfs(node: str, stack: list[str], on_stack: set[str]) -> None:
        for nxt in adj.get(node, ()):
            if nxt in on_stack:
                # Found a back-edge — cycle is stack[idx..] + nxt
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                key = canonical(cycle[:-1])  # exclude closing repeat
                if key not in seen:
                    seen.add(key)
                    out.append(cycle)
                continue
            if nxt in stack:
                continue
            stack.append(nxt)
            on_stack.add(nxt)
            dfs(nxt, stack, on_stack)
            on_stack.remove(nxt)
            stack.pop()

    # DFS from every node that has outgoing edges
    for root in adj:
        dfs(root, [root], {root})

    return out


def format_cycle(path: list[str]) -> str:
    """Render a cycle path for an error message.

    Input: ``[A, B, C, A]`` (start and end repeat to close the loop).
    Output: ``"A → B → C → A"``.
    """
    return " → ".join(path)


def find_cycles_for_edges(
    calls: Iterable[_CallLike],
    owner: str,
    targets: Iterable[str],
    max_depth: int = 64,
) -> dict[str, list[str] | None]:
    """Multi-target cycle check for the swarm/fan-out path.

    Given the existing in-flight graph plus a proposed FAN-OUT
    ``owner → [t1, t2, ...]``, return for each proposed target
    whether ``owner → t`` would close a cycle. Each entry is the
    cycle path (start-to-start, like ``find_cycle``) or ``None``
    if that target is safe.

    All targets are checked against the SAME unmutated snapshot
    of the existing graph. Sibling proposed edges are NOT mixed
    in. Rationale: every sibling has the same ``owner`` as caller,
    so the only way sibling C1 could falsely report a cycle for
    C2 is if ``owner → C1 → ... → owner`` exists — and that's
    exactly what the single-target check on C1 already catches.

    The caller (typically ``ask_agents`` in bus.py) decides what
    to do with the result. Per the design doc, ANY non-None entry
    refuses the entire fan-out so partial dispatch can't produce
    subtly-wrong synthesize-step outputs.

    Composes ``find_cycle`` so the per-target semantics stay
    identical to single-call ``ask_agent``.
    """
    calls_list = list(calls)  # materialise once; each find_cycle iterates
    return {
        target: find_cycle(calls_list, owner, target, max_depth=max_depth) for target in targets
    }
