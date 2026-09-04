# salient-core

**An agent-control kernel for multi-agent systems. We optimize for what agents *can't* do.**

*A **guardrail and permission layer for AI agent harnesses** — sandboxed tool
scopes, default-deny policy, human-in-the-loop approval, and a replayable audit
trail, under Claude Code, Codex, or your own agent loop.*

[![CI](https://github.com/baggybin/salient-core/actions/workflows/ci.yml/badge.svg)](https://github.com/baggybin/salient-core/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

![salient-core — an agent-control kernel](imgs/social-preview.jpg)

Most AI frameworks focus on giving agents more capabilities. `salient-core`
focuses on **proving what they actually did**, and **stopping them from doing
what they shouldn't**. It sits below the LLM — between the model and your
tools — as a **default-deny control kernel**. Whichever harness drives the loop,
the gates are the same.

> Let agents act on your infrastructure, but never outside the box you drew —
> and always keep the receipts.

**Showcase:** [salient-tutor](https://github.com/baggybin/salient-tutor) — a
Socratic teaching agent built on this kernel.

---

## The problem

Right now, most stacks secure agents with system prompts like *"please don't
delete that folder"* or *"be careful with production."* That is **probabilistic
safety**. If the model hallucinates, is manipulated, or simply gets over-eager,
nothing *underneath* the loop enforces the rule — the destructive tool call
still runs.

Orchestrators (LangGraph, CrewAI, AutoGen, …) excel at composing workflows and
roles. They do not put a **transport-neutral, default-deny gate** under every
tool invocation, across SDK built-ins, MCP, bus tools, and model-emitted text.

## The solution

`salient-core` moves control out of the prompt and into the kernel. An agent that
can still run `rm -rf` because a prompt asked it not to is not **sandboxed** — it
is hoping. Every tool call passes through **scope + safeguard gates** *before*
anything executes.
Capability exposure and authorization are separate: enabling a tool never
implicitly authorizes it. Unclassified tools fail closed. A denied call
**never runs**.

Delegation is bus-mediated and operator-visible. Anything that needs a human
lands in a typed **operator inbox** and waits. Every gate decision and tool
I/O is persisted — secrets redacted — so you can reconstruct what happened.

---

## Core features

- **Default-deny policy gates**: Unclassified tools fail closed. Every tool call passes through scope and safeguard checks *before* execution.
- **Operator inbox**: Delegation and policy walls become typed tickets for human operators—no silent failures or free rein.
- **Redacted audit trail**: A fully replayable record of gate decisions and tool I/O, with secrets automatically redacted.
- **Provable stop**: Stop mechanisms that return evidence the agent died, rather than just assuming a prompt instruction was followed.
- **Typed MCP bus**: Inter-agent tools provided seamlessly through a Model Context Protocol (MCP) server.

*For a deep dive into the kernel's capabilities, see the [Detailed Features Table](docs/FEATURES.md).*

---

## Architecture

Every agent runs its own provider loop with a **bus MCP server** attached.
Tool calls hit the gates first; human decisions hit the inbox; learning lands
in the shared KG. The kernel's value is this topology, not any one box.

```
LLM / agent loop
       │  tool calls
       ▼
┌──────────────────────────────┐
│        salient-core          │
│  policy gates · typed bus    │
│  audit trail · operator inbox│
└──────────────────────────────┘
   │            │            │
   ▼            ▼            ▼
 Tools      Other agents   Operator
(scoped)   (bus-mediated)  (typed Q/A)
```

Full data-flow, persistence model, the control ladder, and seams:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
hardening log:
[`docs/KERNEL-HARDENING-v0.6.0.md`](docs/KERNEL-HARDENING-v0.6.0.md).

---

## Where it sits (not another orchestrator)

| | salient-core | LangGraph | CrewAI / AutoGen |
|---|---|---|---|
| **Optimizes for** | operator control over agents | workflow expressiveness | role-based collaboration |
| **Coordination** | typed **MCP bus** per agent | in-process state graph | in-process agent/role objects |
| **Policy / gating** | **default-deny below the model**, every call | prompt- / code-level | prompt-level convention |
| **Human-in-the-loop** | first-class **operator inbox** | interrupts / checkpoints | optional human proxy |
| **Audit** | **redacted, replayable** gate + tool trail | app-level logging | app-level logging |
| **Memory** | **noisy-OR KG** + corroboration | checkpointer state | external add-ons |

Use an orchestrator to *compose* LLM calls. Use this kernel when agents must
be *constrained* — and you need receipts.

**When *not* to use it:** single-agent toys (control-plane overhead), or if you
want a hosted no-code runtime. This is a **library kernel** you wire into your
own daemon. Runtimes that ship today are Claude, OpenAI Codex, and
OpenAI-compatible API brains; anything else means writing an `AgentProvider`
(the seam is real, and a new provider inherits the policy gates automatically).

---

## Requirements

- **Python ≥ 3.11, < 3.14**
- **[`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/) `>=0.2.110,<0.3`**
  (pulled in automatically, alongside `pydantic` and `httpx`). Claude access via
  `ANTHROPIC_API_KEY` or an existing Claude Code OAuth session.
- Optional: `pip install 'salient-core[codex]'` for the OpenAI Codex runner
  (same bus + gates; your own Codex/OpenAI auth).
- The `polybrain` runtime needs no extra install — just an API key for the
  sub-brain you want (`MINIMAX_API_KEY`, `DEEPSEEK_API_KEY`,
  `GLM_API_KEY`/`ZHIPU_API_KEY`).

> **Default-deny, out of the box.** By default, an engagement with no policy refuses **every** tool call. Policy is opt-in-safe on purpose. See [`docs/EXTRACTION.md`](docs/EXTRACTION.md) for how to configure your permissions.

---

## Quick start

### 1. Install

```bash
pip install salient-core
```

### 2. Run the multi-agent showcase (no API key)

Fans one prompt across a panel over the bus, captures each leg, and scores
**semantic convergence** — real `ask_consensus` machinery, offline:

```bash
pip install salient-core starlette uvicorn
cd examples/consensus_panel
uvicorn server:app --reload      # → http://127.0.0.1:8055
```

See [`examples/consensus_panel/`](examples/consensus_panel/README.md) to swap
the mock runner for live models. Full app on the kernel:
[`salient-tutor`](https://github.com/baggybin/salient-tutor).

### 3. Use a standalone module

Some pieces work without the full daemon — e.g. the SM-2 scheduler:

```python
from salient_core.tutor.schedule import next_interval_days, next_mastery

interval = next_interval_days(prev_days=7.0, grade="good")  # → ~16.1
mastery = next_mastery(prev_mastery=0.5, grade="easy")      # → ~0.75
```

---

## Documentation & Advanced Integration

`salient-core` is designed to be wired into your own daemon. We provide comprehensive documentation on how to configure policies, implement protocols, and understand the internal architecture.

- **[Detailed Features Table](docs/FEATURES.md)**
- **[Architecture & Control Ladder](docs/ARCHITECTURE.md)**
- **[Extension & Daemon Integration Guide](docs/EXTRACTION.md)**
- **[Bus Tool Field Reference](docs/BUS_TOOL_FIELDS.md)**

---

## Status

Pre-alpha (`0.8.17`). APIs are evolving; 1090 tests, 66% coverage. See
[`CHANGELOG.md`](CHANGELOG.md).

## Contributing

Kernel changes land **here first**. Public API is guarded by
`tests/test_public_api.py`; new capabilities go through Protocol contracts and
`set_*` seams — not domain specifics baked into the kernel.

```bash
git clone https://github.com/baggybin/salient-core.git
cd salient-core
pip install -e ".[dev]"
pre-commit install
pytest tests/ -q
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

Apache 2.0 — see [`LICENSE`](LICENSE).

---

*Built for constrained multi-agent systems on the Model Context Protocol (MCP) —
agent security, tool-use permissions, and provable operator control over
autonomous agents.*
