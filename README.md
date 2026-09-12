# salient-core

**A permission layer that runs *below* the model, not in its prompt.** Every tool
call — SDK built-in, MCP, inter-agent bus, or one the model tries to slip through
as plain text — hits the same default-deny gate before it executes. A denied call
never runs.

[![CI](https://github.com/baggybin/salient-core/actions/workflows/ci.yml/badge.svg)](https://github.com/baggybin/salient-core/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/baggybin/salient-core/blob/main/LICENSE)

> One developer, pre-alpha (`0.8.24`), not yet on PyPI, APIs still moving. 1137 tests.
> I extracted this from a private multi-agent security orchestrator; the control
> layer turned out to generalize, so it's here under Apache-2.0.

![salient-core — a permission layer below the model](https://raw.githubusercontent.com/baggybin/salient-core/main/imgs/social-preview.jpg)

---

Most stacks secure agents with a system prompt: *"don't touch production,"*
*"don't delete that folder."* That's a request, not a wall. If the model
hallucinates, gets prompt-injected, or is just over-eager, nothing underneath the
loop stops the destructive call — it runs. An agent that can still run `rm -rf`
because a prompt asked it not to isn't sandboxed; it's hoping.

`salient-core` moves the rule out of the prompt and into the call path. It sits
between the model and your tools as a default-deny kernel: every invocation is
classified and checked *before* anything executes, the decision is recorded, and
anything a human needs to approve waits in a typed inbox. Enabling a tool never
implicitly authorizes it — capability and authorization are separate, and
unclassified tools fail closed.

## The one thing a prompt-level guardrail can't do

The gate keys off a *canonical* identity, not the wire name the model chose — so a
call can't rename itself, or switch transports, to dodge the rule. Here the same
policy denies a structured tool call and the model's fallback of re-emitting that
call as plain text. No API key, no daemon:

```python
import anyio
from salient_core.policy import scope
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.scope_api import (
    InvocationIdentity, InvocationTransport, ToolInvocation, evaluate_scope,
)

# One policy: `context_write` may only write values that resolve to an in-scope
# host. Nothing is added to scope, so every host is out of scope.
dataset = PolicyDataset(
    tool_targets={"bus.context_write": scope.ExtractorSpec(fields={"value": "host"})},
    prohibited_patterns={}, loud_patterns={},
)
store = scope.ScopeStore(None, "agent")  # in-memory audit

def call(transport, args):
    identity = InvocationIdentity(
        transport=transport, wire_name="context_write",
        qualified_name="bus.context_write", agent_id="agent",
    )
    return ToolInvocation.normalize(identity, args)

async def main():
    args = {"key": "finding", "value": "prod.internal", "token": "sk-live-42"}
    structured = await evaluate_scope(call(InvocationTransport.MCP, args), store, dataset)
    as_text    = await evaluate_scope(call(InvocationTransport.TEXT, args), store, dataset)
    print(structured.allowed, as_text.allowed)   # -> False False  (same policy, both denied)

anyio.run(main)
```

The end-to-end version — the model literally emitting
`<function=context_write>…</function>` text and the runner denying it before any
mutation — is
[`tests/test_policy_cross_transport.py`](https://github.com/baggybin/salient-core/blob/main/tests/test_policy_cross_transport.py).

### The receipt it leaves

Every decision is persisted, secrets redacted, so you can reconstruct what
happened. That same denial writes a row like:

```json
{
  "agent": "agent",
  "tool": "context_write",
  "verdict": "deny",
  "reason": "engagement has no scope set. Run: `salientctl scope add <pattern> …`",
  "targets_json": [{"kind": "host", "value": "prod.internal", "source_field": "value"}],
  "args_json": {"key": "finding", "value": "prod.internal", "token": "<redacted-secret>"}
}
```

Note the `token`: redaction runs on the audit projection, so a credential in the
arguments never lands in the log.

## What's in the box

- **Default-deny gate.** Every tool call is classified and checked before it runs.
  Unclassified tools fail closed. Enabling a tool is not authorizing it.
- **Transport-neutral.** SDK built-ins, MCP tools, the inter-agent bus, and
  model-emitted text all go through one policy object, keyed off a canonical name.
- **Operator inbox.** Anything an agent isn't allowed to decide alone becomes a
  typed question and waits for a human — no silent failure, no free rein.
- **Redacted, replayable audit.** Gate decisions and tool I/O are persisted with
  secrets stripped, so a run can be reconstructed after the fact.
- **Provable stop.** Stopping an agent returns evidence it actually died, rather
  than trusting that a prompt instruction was obeyed.
- **Typed MCP bus + shared knowledge graph.** Agents coordinate over an MCP bus
  rather than an in-process call graph, and what they learn persists in a
  cross-session KG with noisy-OR corroboration.

Deeper reference: [`docs/FEATURES.md`](https://github.com/baggybin/salient-core/blob/main/docs/FEATURES.md).

## Not an orchestrator

LangGraph, CrewAI, and AutoGen *compose* the loop — roles, workflows, state. This
*gates* it. They're complementary: run an orchestrator to decide what the agents
do, run this to bound what they're allowed to do while they do it. The value here
is the control topology, not workflow expressiveness.

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

Full data-flow, persistence model, and the control ladder:
[`docs/ARCHITECTURE.md`](https://github.com/baggybin/salient-core/blob/main/docs/ARCHITECTURE.md).

## What doesn't work yet (read this)

- **It's a policy kernel, not a sandbox.** It gates calls that route *through* it.
  It does not contain code that has already escaped the process — if an agent
  shells out to something that ignores the gate, the gate can't help. Scope it to
  tools you route.
- **It's a library you wire into your own daemon**, not a hosted runtime and not a
  no-code product. Single-agent workflows pay control-plane overhead for a trust
  boundary they don't have — the payoff is multi-agent.
- **Runtime maturity is uneven.** The Claude runtime (via `claude-agent-sdk`) is
  the most exercised. The OpenAI Codex runner and the OpenAI-compatible
  `polybrain` brains are newer and less battle-tested. A new runtime means writing
  an `AgentProvider`; it inherits the gates automatically.
- **No clean one-call decision seam yet.** `evaluate_scope` above is the real,
  importable entry point, but the three-way allow/deny/inbox routing isn't a
  single tidy `decide()` function. On the roadmap.
- **Not on PyPI.** You install from a git ref (below). Pin a commit.

## Before you trust it

You'd be `pip install`-ing an unpinned pre-alpha into a process that holds your
credentials. Fair to be wary. Two things help: the scope decision core
(`evaluate_scope`) imports and runs without the daemon, so you can read and
exercise the gate in isolation — that whole example above needs no engagement, no
API key; and the threat model lives in
[`SECURITY.md`](https://github.com/baggybin/salient-core/blob/main/SECURITY.md).
Pin a commit you've read.

## Try it

```bash
# not on PyPI — install from a pinned git ref
pip install "git+https://github.com/baggybin/salient-core.git@main"
```

Then run the offline multi-agent showcase — fans one prompt across a panel over
the bus, captures each leg, and scores semantic convergence, all with a mock
runner so no API key is needed:

```bash
pip install starlette uvicorn
cd examples/consensus_panel
uvicorn server:app --reload      # -> http://127.0.0.1:8055
```

Swap the mock for live models per
[`examples/consensus_panel/`](https://github.com/baggybin/salient-core/blob/main/examples/consensus_panel/README.md).

## Built on it

- **[salient-tutor](https://github.com/baggybin/salient-tutor)** — a Socratic
  teaching agent; a full application running on the kernel.
- **salient-assay** — a new blue-team (defensive) hunt tool built on the kernel.
  Hunter, analyst, and sceptic agents comb a codebase together under these same
  gates, cross-checking findings with evidence and a veto step to hold down false
  positives. Intended for public release, on top of this kernel, once it's ready.

## Requirements

- Python ≥ 3.11, < 3.14
- [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/)
  `>=0.2.110,<0.3` (pulled in automatically with `pydantic` and `httpx`). Claude
  access via `ANTHROPIC_API_KEY` or an existing Claude Code OAuth session.
- Optional Codex runner:
  `pip install 'salient-core[codex] @ git+https://github.com/baggybin/salient-core.git'`
  (bring your own Codex/OpenAI auth).
- The `polybrain` runtime needs only an API key for the sub-brain you want
  (`MINIMAX_API_KEY`, `DEEPSEEK_API_KEY`, `GLM_API_KEY`/`ZHIPU_API_KEY`).

> **Default-deny out of the box.** An engagement with no policy refuses *every*
> tool call — opt-in-safe on purpose. Configuring permissions:
> [`docs/EXTRACTION.md`](https://github.com/baggybin/salient-core/blob/main/docs/EXTRACTION.md).

## Status

Pre-alpha (`0.8.24`). APIs are evolving. 1137 tests, 67% coverage overall —
concentrated in the policy/gate core, which is what I'd trust most today. See
[`CHANGELOG.md`](https://github.com/baggybin/salient-core/blob/main/CHANGELOG.md).

## More docs

- [Architecture & control ladder](https://github.com/baggybin/salient-core/blob/main/docs/ARCHITECTURE.md)
- [Detailed feature table](https://github.com/baggybin/salient-core/blob/main/docs/FEATURES.md)
- [Extension & daemon integration](https://github.com/baggybin/salient-core/blob/main/docs/EXTRACTION.md)
- [Bus tool field reference](https://github.com/baggybin/salient-core/blob/main/docs/BUS_TOOL_FIELDS.md)

## Contributing

Kernel changes land here first. The public API is guarded by
`tests/test_public_api.py`; new capabilities go through Protocol contracts and
`set_*` seams, not domain specifics baked into the kernel.

```bash
git clone https://github.com/baggybin/salient-core.git
cd salient-core
pip install -e ".[dev]"
pre-commit install
pytest tests/ -q
```

See [`CONTRIBUTING.md`](https://github.com/baggybin/salient-core/blob/main/CONTRIBUTING.md).

## License

Apache 2.0 — see [`LICENSE`](https://github.com/baggybin/salient-core/blob/main/LICENSE).
