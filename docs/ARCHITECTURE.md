# Architecture

Internal-facing reference for someone reading or extending the kernel.

## Module map

```
src/salient_core/
├── protocols.py          DaemonServices, ToolBuilder, AliasProtocol, AgentBackend
├── alias.py              IdentityAlias (no-op default) + module-level passthrough
├── display.py            ANSI helpers (NO_COLOR aware)
│
├── coord/
│   ├── questions.py      QuestionInbox — operator question/answer inbox
│   ├── delegations.py    Reach extractor — parse agent reach from prompts
│   └── delegation_graph.py  Cycle detection in the delegation graph
│
├── memory/
│   ├── kg.py             KnowledgeGraph — noisy-OR corroboration, TTL, embeddings
│   ├── actions.py        ActionLedger — persistent tool-call history
│   ├── embeddings.py     Provider-agnostic embedder (inert by default)
│   ├── compaction.py     Archive-first compaction for KG + context
│   └── lessons.py        Per-agent lessons store
│
├── policy/
│   ├── scope.py          Scope gate — target extraction + allow/deny per tool call
│   ├── safeguards.py     Safeguards engine — posture + pattern matching
│   ├── registry.py       Active PolicyDataset registry (set_active seam)
│   ├── defaults.py       Empty default scope/safeguard dataset
│   └── _safeguard_vocab.py  Encoded trigger vocabulary (injectable)
│
├── bus/                  The inter-agent bus (single MCP server per agent)
│   ├── __init__.py       make_bus(daemon, owner, *, extra_tools=…) + set_bus_builder seam
│   ├── _common.py        bus_tool decorator, shared helpers, set_bus_skin_modules seam
│   ├── _flags.py         BusFlags — typed .trusted routing/write-back channel
│   ├── _context.py       context_write/read/list/grep/section/head/tail
│   ├── _delegation.py    ask_agent/ask_agents/ask_partner/ask_operator (+ observer/disabled seams)
│   ├── _consensus.py     ask_consensus — multi-model agreement
│   ├── _discovery.py     list_agents/search_skills/get_skill
│   ├── _kg.py            kg_assert/query/neighbors/stats/semantic_query/record_review (+ assert hook)
│   ├── _lessons.py       propose_lesson
│   ├── _lifecycle.py     spawn_template/swarm_finish
│   ├── _skills.py        propose_skill
│   ├── _audit.py         read_evidence/prior_actions/prior_techniques
│   ├── _credentials.py   cred_record/cred_search/get_credential
│   └── _context_store.py SQLite WAL context store + meta-KV
│
├── daemon/
│   ├── __init__.py       Public API re-exports
│   ├── runner.py         AgentRunner — Claude SDK response loop
│   ├── _runner_factory.py  Runner construction, tool wiring, hook setup, _launch_profile injection
│   ├── _tool_registry.py Tool-builder / wire-name / daemon-skin-module seams
│   ├── _backend.py       AgentBackend abstraction (v2 multi-SDK seam)
│   ├── _event_hub.py     Fan-out event hub with replay support
│   ├── _tasks.py         Background task spawning
│   ├── _helpers.py       Job, BusCall dataclasses, shared utilities
│   ├── _prompts.py       Prompt-addendum loader + thinking-provider / prompts-root seams
│   └── _questions.py     Question/answer RPC handler + operator-authz seam
│
├── ask_fable/            Gated Fable (claude-fable-5) reasoning MCP tool
│
└── tutor/
    └── schedule.py       SM-2 / FSRS-lite spaced-repetition scheduler
```

## Import direction

One-way down the stack:

```
protocols → coord → memory → policy → bus → daemon
                    ↓
                 display (standalone)
```

The bus imports the Daemon type only inside `TYPE_CHECKING` guards. The
runtime dependency is reverse — the daemon calls `bus.make_bus(self, name)`
and the closures capture the daemon reference.

## Seams

The kernel carries only generic mechanism — no app-specific ("skin") code. It
plugs into a downstream in two ways.

### Protocol contracts

Four Protocols in `protocols.py` (and `daemon/_backend.py`) define the typed
surfaces a downstream implements:

1. **DaemonServices** — the bounded Daemon surface a runner may touch
   (`profile`, `engagement_path`, `context`, `kg`, `inbox`, `add_question`,
   `event_hub` + `subscribe_events`/`unsubscribe_events`).

2. **ToolBuilder** — callable that builds a tool MCP server from a factory
   type + config. The kernel ships a stub; the downstream provides the real
   implementation.

3. **AliasProtocol** — tool-name remapping between the wire names a model sees
   and the kernel's internal names. The kernel ships `IdentityAlias`
   (passthrough); a downstream that needs custom tool-name mapping calls
   `alias.set_active(RealAlias())`.

4. **AgentBackend** — abstract SDK backend (v1: Claude SDK; v2 seam for
   multi-SDK support).

### Runtime registration seams

The dominant idiom: a `set_*` function read at **call time** (never bound at
import time), each with a safe default so the kernel stays runnable standalone.
A downstream skin calls the relevant `set_*` at startup. Defaults are either a
raising stub (fail loud if a required provider is missing) or a permissive
no-op.

A second family — `register_*` — is **additive** rather than provider-replacing:
each extends a generic built-in set (credential kinds, redaction field names,
credential-tool markers, scope-extractor kinds, swarm-prompt guidance) with a
skin's domain vocabulary, so the kernel ships a working generic default and a
skin layers its specifics on top. Same call-time idiom, called once at startup.

| Seam | Module | Default |
|---|---|---|
| `set_tool_builder` | `daemon/_tool_registry.py` | raising stub (fail-loud) |
| `set_tool_wire_names` | `daemon/_tool_registry.py` | empty → omits primary-tool line |
| `set_daemon_skin_modules` | `daemon/_tool_registry.py` | none registered |
| `set_thinking_provider` | `daemon/_prompts.py` | claims no model (static config) |
| `set_prompts_root` | `daemon/_prompts.py` | packaged `prompts/` dir |
| `set_authz_provider` | `daemon/_questions.py` | permissive no-op |
| `set_delegation_observer` | `bus/_delegation.py` | no-op |
| `set_agent_disabled_checker` | `bus/_delegation.py` | never disabled |
| `set_kg_assert_hook` | `bus/_kg.py` | no-op |
| `set_bus_skin_modules` | `bus/_common.py` | none registered |
| `set_bus_builder` | `bus/__init__.py` | default `make_bus` |
| `alias.set_active` | `alias.py` | `IdentityAlias` passthrough |
| `policy.registry.set_active` | `policy/registry.py` | empty scope/safeguard dataset |
| `register_extractor` | `policy/scope.py` | generic kinds; unknown kind fails closed |
| `register_credential_vocab` | `memory/credentials.py` | generic kinds (password/ssh_key/api_token) |
| `register_secret_fields` | `bus/_common.py` | generic secret field names |
| `register_cred_tool_markers` | `bus/_common.py` | generic markers (cred_record/cred_search) |
| `register_swarm_bootstrap_addendum` | `daemon/_prompts.py` | none — generic swarm guidance only |

### `_launch_profile` — per-agent privilege separation

The daemon injects an agent's `launch:` block from `agents.yaml` into
`factory_config` under the opaque key `_launch_profile`
(`daemon/_runner_factory.py`). The kernel **never interprets it** — the tool
builder (skin-side) resolves it to a capability-scoped subprocess launcher.
Absent `launch:` ⇒ the key is not injected ⇒ unprivileged default. It mirrors
the existing daemon-injection convention for `_posture` / `_scope_networks` /
`_authed_sessions`.

## Data flow

```
Operator message
    ↓
Daemon.prompt(agent, message)
    ↓
AgentRunner.submit(message) → queue
    ↓
AgentRunner loop:
    1. Load context (ContextStore)
    2. Call Claude SDK with system prompt + bus MCP server
    3. SDK calls tools:
       a. Bus tools (ask_agent, kg_assert, record_review, etc.)
       b. Tool-builder tools (downstream-provided; run in a
          privilege-separated subprocess when a `_launch_profile` is present)
    4. Each tool call passes through scope + safeguards gates
    5. Cross-agent delegations land in the operator's QuestionInbox
    6. Response text → evidence capture → KG updates
    ↓
Job.result.text → caller
```

## Persistence

All state is SQLite:

| Store | File | Contents |
|---|---|---|
| ContextStore | `context.db` | Methodology notes, meta-KV, event log |
| KnowledgeGraph | `kg.db` | Facts with noisy-OR corroboration, embeddings, TTL |
| QuestionInbox | `questions.db` | Operator questions + answers |
| ActionLedger | `actions.db` | Tool-call history |

## Testing

443 tests, ~39% overall coverage (`pytest tests/ --cov=salient_core`). Bus
wire schemas are additionally pinned byte-for-byte by golden-master snapshots
(`tests/golden/bus_schemas/`, see `docs/BUS_TOOL_FIELDS.md`).

Core standalone modules have strong coverage: KG (89%), actions (87%).
Integration-heavy modules that need a live Daemon harness are still thin
(scope ~18%, safeguards ~46%, runner ~26%) — coverage rises as the kernel
fills out.
