# Expanding the "single socket" without losing speed

## Context

This note answers "how do we expand on the single-socket design without losing its
speed?" First, what "the single socket" actually is in this repo:

1. **The literal socket** (`/tmp/salient.sock`) is only a constant here
   (`src/salient_core/daemon/_helpers.py:65` — `DEFAULT_SOCKET`). The bind/listen/accept
   code lives in the downstream private `salient` app's `daemon.py`, not in `salient-core`.
2. **The thing that plays the "single socket" role in-core** is the bus: one in-process
   MCP server per agent (`src/salient_core/bus/__init__.py:198`, `create_sdk_mcp_server`)
   holding ~40 tool closures, all running on a single asyncio loop.

**Why it's fast — the invariants to protect:**
- Inter-agent tool calls are plain Python closures — **zero serialization, zero socket
  hops** on the hot path.
- One event loop, one job at a time per agent (`daemon/runner.py:1501` `_run` loop with
  a priority steer lane), so no lock contention on dispatch.
- Observers never slow producers: `EventHub` (`daemon/_event_hub.py`) fans out via
  bounded queues with **drop-on-full backpressure** plus a replay ring.
- Blocking SQLite goes to threads; the loop never blocks.

**The principle for expansion:** *tiered transport.* Keep the closure fast path untouched;
anything that crosses a boundary (new client, remote agent, new UI) attaches at an existing
seam — never inline in the dispatch path.

## Recommended approach

Expand at the three seams the codebase already provides, in this order of value:

### 1. New bus channels (zero speed cost — pure fast-path growth)
New capability = new `make_*_tools` factory following the existing pattern
(`bus/_context.py`, `bus/_kg.py`, `bus/_consensus.py` are the templates):
- Add `src/salient_core/bus/_<channel>.py` with a `make_<channel>_tools(daemon, owner)` factory.
- Append names to `_BUS_TOOL_NAMES` and closures to `bus_tool_fns` in `make_bus`
  (`bus/__init__.py:43-80` and `:160-197`) — order must match 1:1.
- Tools stay closures; still no socket involved. `make_bus_tools` mirrors them onto the
  agent's second namespace automatically.

### 2. Multi-client observation (speed-safe by construction)
Any number of new consumers (web overlays, tailers, dashboards, a metrics exporter) attach
via `EventHub.subscribe()` (`daemon/_event_hub.py:38`) — the drop-on-full contract means a
slow or remote subscriber can never stall an agent. This is the correct attach point for
anything "read-only over the wire": a downstream socket/WebSocket endpoint just wraps a
subscriber queue. No hot-path change.

### 3. Remote/cross-boundary calls (the only place a socket ever appears)
If/when agents or clients on another process/machine are wanted:
- Define the boundary as a new method surface on the `DaemonServices` protocol
  (`src/salient_core/protocols.py`) — the same seam the bus tools already call through.
- A remote transport becomes *an implementation of DaemonServices* that proxies over a
  socket; local deployments keep the direct in-process implementation. Local calls stay
  closures; only genuinely remote calls pay serialization.
- The `AgentBackend` protocol (documented in `docs/ARCHITECTURE.md:73-90` as the "v2
  multi-SDK seam") is the analogous seam for remote *agents*.
- The actual socket server (accept loop, framing) belongs downstream with the existing
  `daemon.py`; in-core we only ship the protocol + a reference in-process implementation.

## What NOT to do
- Don't route local tool calls through any serialized transport "for uniformity" — that
  trades the design's core advantage for symmetry.
- Don't add unbounded or blocking fan-out; every new subscriber must inherit the
  drop-on-full contract.
- Don't put bind/listen code in salient-core; the transport layer is downstream by design.

## Status
The first increment of **(2)** is implemented: `DaemonServices` declares `event_hub` and
the `subscribe_events()` / `unsubscribe_events()` seam (`src/salient_core/protocols.py`),
with contract tests (reference implementation + drop-on-full) in
`tests/test_event_hub.py`. Downstream socket/WebSocket relays attach there. Steps (1) and
(3) remain design guidance for future work.
