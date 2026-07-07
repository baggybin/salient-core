# salient-core

> A multi-agent coordination kernel with policy gates, knowledge graph,
> and operator-mediated delegation.

![salient-core: a single inter-agent bus at the hub, with agents fanned out on its spokes over a knowledge graph](imgs/hero-bus.jpg)

[![CI](https://github.com/baggybin/salient-core/actions/workflows/ci.yml/badge.svg)](https://github.com/baggybin/salient-core/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

`salient-core` is a generic agent-coordination kernel — the infrastructure
for running multiple Claude agents concurrently, each scoped to a single
tool surface, coordinated through a typed inter-agent bus with
operator-mediated approval gates.

The kernel was extracted from Salient, a multi-agent security orchestrator
(private). The security-specific code stayed behind; what's here is the
coordination layer that generalizes to any domain.

**Showcase application:** [salient-tutor](https://github.com/baggybin/salient-tutor) —
a Socratic teaching agent built on this kernel.

## What's in the kernel

| Component | What it does |
|---|---|
| **Bus-as-MCP** | ~40 typed inter-agent tools (delegation, context, KG, discovery, audit) exposed as a single MCP server per agent, with an `extra_tools` slot for domain add-ons |
| **Noisy-OR KG** | Cross-session knowledge graph with corroboration, embeddings, and archive-first compaction |
| **Policy gates** | Scope + safeguards enforced *below* the model — default-deny on every tool invocation |
| **Operator inbox** | Typed question/answer pattern for anything that needs a human decision |
| **SM-2 scheduler** | Spaced-repetition gradebook for durable recall tracking |
| **Runner** | Claude-SDK-specific agent runner (v1), behind a `DaemonServices` Protocol seam for multi-SDK v2; per-agent tool subprocesses can be privilege-separated via an opaque `_launch_profile` seam |

## Seams

The kernel ships no app-specific ("skin") code. Instead it exposes two kinds of
plug-in points, and a downstream application (the security skin, the tutor
showcase, or your own project) fills them in at startup:

- **Protocol contracts** — the typed surfaces a downstream implements
  (`DaemonServices`, `ToolBuilder`, `AliasProtocol`, `AgentBackend` in
  `salient_core.protocols`).
- **Runtime registration seams** — a family of `set_*` functions read at *call
  time* (never import time), each with a safe default so the kernel stays
  runnable standalone (e.g. `set_bus_builder`, `set_tool_builder`,
  `set_thinking_provider`, `set_kg_assert_hook`, `alias.set_active`).

```python
from salient_core.protocols import DaemonServices, ToolBuilder, AliasProtocol

class MyDaemon:
    """A downstream daemon implements DaemonServices."""
    profile: dict
    engagement_path: Path | None
    context: ContextStore
    kg: KnowledgeGraph
    inbox: QuestionInbox

    def add_question(self, agent: str, question: str, job_id: int | None = None) -> int: ...
```

See [`docs/EXTRACTION.md`](docs/EXTRACTION.md) for the full guide and the
complete seam catalogue in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quick start

```bash
pip install salient-core
```

```python
from salient_core.memory.kg import KnowledgeGraph
from salient_core.tutor.schedule import next_interval_days, next_mastery
from salient_core.coord.questions import QuestionInbox

# The scheduler is standalone — use it without the full daemon
interval = next_interval_days(prev_days=7.0, grade="good")  # → ~16.1
mastery = next_mastery(prev_mastery=0.5, grade="easy")      # → ~0.75
```

For a full working example, see
[`salient-tutor`](https://github.com/baggybin/salient-tutor).

## Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the module map,
data flow, and Protocol seams.

## Status

Pre-alpha. APIs are evolving. See [`CHANGELOG.md`](CHANGELOG.md) for release
history.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
