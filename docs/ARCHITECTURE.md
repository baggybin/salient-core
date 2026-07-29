# Architecture

Internal-facing reference for someone reading or extending the kernel.

## Module map

```
src/salient_core/
├── protocols.py          DaemonServices, ToolBuilder, ToolBundleBuilder,
│                         ToolBuildContext, AliasProtocol (re-exports
│                         AgentBackend / ReapableBackend from runtime)
├── runtime.py            Provider-neutral runtime contracts: AgentTool /
│                         ToolBundle, the AgentEvent union, TurnUsage,
│                         AgentBackend, ReapableBackend, PolicyDenied, and
│                         gate_tool_bundle (the provider policy gate)
├── providers.py          AgentProvider Protocol, ProviderRegistry, entry-point
│                         discovery (`salient.agent_providers`), builtin registry
├── alias.py              IdentityAlias (no-op default) + module-level passthrough
├── display.py            ANSI helpers (NO_COLOR aware)
├── codex.py              CodexBackend / CodexProvider — OpenAI Codex runtime,
│                         approval plumbing, read-only command classifier
├── codex_mcp.py          MCP gateway so a codex agent can call bus tools
│
├── coord/
│   ├── questions.py      QuestionInbox — operator question/answer inbox
│   ├── delegation_graph.py  Cycle detection in the in-flight delegation graph
│   └── reconstruct.py    build_reconstruction — one turn's causal chain from
│                         its correlation_id, with a mirror ⊆ scope.db check
│
├── memory/
│   ├── kg.py             KnowledgeGraph — noisy-OR corroboration, TTL, embeddings
│   ├── actions.py        ActionLedger — persistent tool-call history
│   ├── embeddings.py     Provider-agnostic embedder (inert by default)
│   ├── recall.py         semantic_recall — embed-then-query convenience
│   ├── compaction.py     Archive-first compaction for KG + context
│   ├── credentials.py    Credential-vocabulary seam (register_credential_vocab)
│   └── lessons.py        Per-agent lessons store
│
├── policy/
│   ├── scope.py          ScopeStore, extractors, target extraction + allow/deny
│   ├── scope_evaluation.py  Pure, transport-neutral evaluate_scope
│   ├── scope_audit.py    Raw-vs-redacted dual audit snapshots
│   ├── scope_placeholders.py  Fail-closed unresolved-placeholder detection
│   ├── scope_api.py      SCOPE_API_VERSION facade for downstream extractors
│   ├── _scope_schema.py  SQLite schema lifecycle for the scope store
│   ├── _authorization_snapshot.py  Versioned, content-addressed AuthorizationSnapshot
│   ├── resource_identity.py  Canonical repo: / cloud: / saas: identities
│   ├── safeguards.py     Safeguards engine — posture, patterns, operator-prompt modes
│   ├── safeguard_evaluation.py  Pure safeguard + sticky-halt evaluation
│   ├── decision.py       Normalized ToolInvocation + neutral PolicyDecision types
│   ├── redaction.py      Structural redaction + register_secret_fields /
│   │                     register_cred_tool_markers
│   ├── registry.py       Active PolicyDataset registry (set_active seam)
│   └── defaults.py       Generic defaults, incl. known qualified SDK classifications
│
├── bus/                  The inter-agent bus (single MCP server per agent)
│   ├── __init__.py       make_bus / make_bus_tools / make_bus_tool_bundle
│   │                     + set_bus_builder seam
│   ├── _common.py        bus_tool decorator, shared helpers, set_bus_skin_modules
│   ├── _flags.py         BusFlags — typed .trusted routing/write-back channel
│   ├── _context.py       context_write/read/list/grep/section/head/tail/lines/
│   │                     count/summary
│   ├── _delegation.py    ask_agent/ask_agents/ask_partner/ask_operator
│   │                     (+ observer / disabled-checker seams)
│   ├── _consensus.py     ask_consensus — multi-model agreement
│   ├── _discovery.py     list_agents/search_skills/get_skill
│   ├── _kg.py            kg_assert/query/neighbors/stats/semantic_query/
│   │                     record_review (+ assert hook)
│   ├── _lessons.py       propose_lesson
│   ├── _lifecycle.py     spawn_template/swarm_finish
│   ├── _skills.py        propose_skill
│   ├── _audit.py         rule_validate/read_evidence/prior_actions
│   └── _context_store.py SQLite WAL context store + meta-KV + turn-keyed tables
│
├── daemon/
│   ├── __init__.py       Public API re-exports
│   ├── runner.py         AgentRunner — provider-neutral response loop, budget
│   │                     park/resume, quiesce + QuiescenceReport
│   ├── _runner_factory.py  Runner construction, tool wiring, every PreToolUse
│   │                     hook, provider-bundle gating, _launch_profile injection
│   ├── _backend.py       LocalClaudeBackend + ClaudeProvider (the default runtime)
│   ├── _budget.py        BudgetLedger / evaluate_budget — token-ceiling mechanism
│   ├── proc_registry.py  Per-runner tool-subprocess registry (quiescence tier 1)
│   ├── cgroups.py        cgroup v2 subtree reaping (quiescence tier 2)
│   ├── _policy_hook_adapter.py  SDK/MCP hook payload ↔ ToolInvocation, replay cache
│   ├── _text_policy.py   authorize_text — the model-emitted-text transport
│   ├── _event_hub.py     Fan-out event hub with replay support
│   ├── _tasks.py         spawn_background / track_background / join_background_tasks
│   ├── _helpers.py       Job, BusCall dataclasses, shared utilities
│   ├── _prompts.py       Prompt-addendum loader + thinking-provider / prompts-root seams
│   ├── _questions.py     Question/answer RPC handler + operator-authz seam
│   └── _tool_registry.py Tool-builder / bundle-builder / KG-builder / wire-name /
│                         daemon-skin-module seams
│
├── polybrain/            Multi-vendor OpenAI-compatible API-brain runtime
│   ├── provider.py       PolybrainProvider — registered builtin, env-key probe
│   ├── backend.py        PolybrainBackend — owns its own multi-turn tool loop
│   ├── factory.py        Sub-brain registry (minimax / deepseek / glm)
│   ├── openai_compat.py  httpx chat.completions client (non-streaming v1)
│   ├── models.py         Static model registry + context windows
│   └── types.py          Brain / ChatMessage / AssistantReply / Usage
│
├── worker_protocol/      Wire protocol for reverse-WSS remote tool workers
│   ├── codec.py          Length-prefixed frame codec (uint32 BE + JSON)
│   ├── types.py          hello / ping / call / result / error / control messages
│   ├── session.py        MultiplexSession — in-flight tracking, control priority
│   └── fake.py           In-process fake worker for protocol tests
│
└── tutor/
    ├── schedule.py       SM-2 / FSRS-lite spaced-repetition scheduler
    └── profile.py        bucketed_profile — learner-gradebook read-time view
```

## Import direction

One-way down the stack:

```
runtime → protocols → providers → coord → memory → policy → bus → daemon
                                              ↓
                                          display (standalone)
```

`runtime.py` is the floor: it carries the provider-neutral contracts
(`AgentTool`, `ToolBundle`, `AgentEvent`, `AgentBackend`, `gate_tool_bundle`)
that both `protocols.py` and every provider package import, and it imports
nothing from the kernel itself. `polybrain/` and `codex.py` sit beside the
daemon — they depend on `runtime`, not on `daemon` — and `providers.py` pulls
them in lazily inside `builtin_provider_registry()` so importing the registry
type doesn't drag in an optional runtime.

The bus imports the Daemon type only inside `TYPE_CHECKING` guards. The
runtime dependency is reverse — the daemon calls `bus.make_bus(self, name)`
and the closures capture the daemon reference.

## Seams

The kernel carries only generic mechanism — no app-specific ("skin") code. It
plugs into a downstream in two ways.

### Protocol contracts

The typed surfaces a downstream implements:

1. **DaemonServices** (`protocols.py`) — the bounded Daemon surface the bus
   tools and a runner may touch. It is deliberately the *whole* surface the
   bundled bus tools reach, not just the runner's read-only slice: stores
   (`profile`, `engagement_path`, `context`, `kg`, `inbox`, `actions`,
   `runners`, `all_cfgs`, `event_hub`), the operator-approval question
   methods, the in-flight `bus_call_*` registry, redispatch accounting,
   `budget_charge`, and agent lifecycle.

2. **ToolBuilder** — callable that builds a tool *MCP server* from a factory
   type + config, for the Claude-SDK path. The kernel ships a raising stub.

3. **ToolBundleBuilder** — the provider-neutral sibling: builds a
   `ToolBundle` (plain `AgentTool` handlers) from a factory type + config plus
   a `ToolBuildContext`. This is what codex, polybrain, and any registered
   provider consume, since they have no SDK MCP server to attach to.

4. **AliasProtocol** — tool-name remapping between the wire names a model sees
   and the kernel's internal names. The kernel ships `IdentityAlias`
   (passthrough); a downstream calls `alias.set_active(RealAlias())`.

5. **AgentBackend** (`runtime.py`) — the provider-neutral agent runtime:
   `connect` / `query` / `receive_response` / `interrupt` / `disconnect` /
   `get_context_usage` / `diagnose_failure`, streaming a normalized
   `AgentEvent` union. **ReapableBackend** is an *optional* extension
   (`child_pid` / `child_alive`); a backend that isn't one degrades to
   `sdk_state="no_pid"` in the quiescence report rather than crashing.

6. **AgentProvider** (`providers.py`) — names a backend, declares
   `ProviderCapabilities`, `probe()`s for availability, and `create_backend`s.
   Claude, Codex, and Polybrain are registered builtins;
   `ProviderRegistry.load_entry_points()` discovers third-party providers from
   the `salient.agent_providers` entry-point group.

### Runtime registration seams

The dominant idiom: a `set_*` function read at **call time** (never bound at
import time), each with a safe default so the kernel stays runnable standalone.
A downstream skin calls the relevant `set_*` at startup. Defaults are usually a
raising stub (fail loud if a required provider is missing) or a permissive
no-op; the exception is `set_kg_builder`, whose default actually **builds** (the
kernel has a perfectly good local SQLite store), so the seam only exists for a
downstream to substitute an alternative — e.g. a network-backed KnowledgeGraph.

A second family — `register_*` — is **additive** rather than provider-replacing:
each extends a generic built-in set (credential kinds, redaction field names,
credential-tool markers, scope-extractor kinds, KG source-ref schemes,
swarm-prompt guidance) with a skin's domain vocabulary, so the kernel ships a
working generic default and a skin layers its specifics on top. Same call-time
idiom, called once at startup.

**Lifecycle contract — background tasks.** `daemon/_tasks.py` exposes
`spawn_background` (create + park a fire-and-forget task), `track_background`
(park an already-created task whose handle the caller keeps — e.g. to await it
shielded with a bound), and `join_background_tasks(timeout)`. A downstream daemon
MUST call `join_background_tasks()` during shutdown **before it tears down agent
backends**: `ask_agent`'s non-detached child-stop is a tracked task that calls
`runner.cancel_job` → `backend.interrupt()`, so joining after backend teardown
would drop it mid-interrupt.

| Seam | Module | Default |
|---|---|---|
| `set_tool_builder` | `daemon/_tool_registry.py` | raising stub (fail-loud) |
| `set_tool_bundle_builder` | `daemon/_tool_registry.py` | raising stub (fail-loud) |
| `set_tool_wire_names` | `daemon/_tool_registry.py` | empty → omits primary-tool line |
| `set_daemon_skin_modules` | `daemon/_tool_registry.py` | none registered |
| `set_kg_builder` | `daemon/_tool_registry.py` | builds local SQLite `KnowledgeGraph` (consumed downstream) |
| `set_thinking_provider` | `daemon/_prompts.py` | claims no model (static config) |
| `set_prompts_root` | `daemon/_prompts.py` | packaged `prompts/` dir |
| `set_authz_provider` | `daemon/_questions.py` | permissive no-op |
| `set_spawn_observer` | `daemon/_runner_factory.py` | no-op |
| `set_cgroup_reaper` | `daemon/proc_registry.py` | none → Tier-1 (pid registry) only |
| `set_delegation_observer` | `bus/_delegation.py` | no-op |
| `set_agent_disabled_checker` | `bus/_delegation.py` | never disabled |
| `set_kg_assert_hook` | `bus/_kg.py` | no-op |
| `set_bus_skin_modules` | `bus/_common.py` | none registered |
| `set_bus_builder` | `bus/__init__.py` | default `make_bus` |
| `set_provider_registry` | `providers.py` | lazily built builtin registry (Claude + Codex + Polybrain + entry points) |
| `alias.set_active` | `alias.py` | `IdentityAlias` passthrough |
| `policy.registry.set_active` | `policy/registry.py` | generic safeguards, bus targets, and known qualified SDK classifications |
| `register_extractor` | `policy/scope.py` | generic kinds; unknown kind fails closed |
| `register_credential_vocab` | `memory/credentials.py` | generic kinds (password/ssh_key/api_token) |
| `register_source_resolver` | `memory/kg.py` | no schemes registered |
| `register_secret_fields` | `policy/redaction.py` (re-exported from `bus/_common.py`) | generic secret field names |
| `register_cred_tool_markers` | `policy/redaction.py` (re-exported from `bus/_common.py`) | generic markers (cred_record/cred_search) |
| `register_swarm_bootstrap_addendum` | `daemon/_prompts.py` | none — generic swarm guidance only |

### Tool-authorization boundary

Every tool invocation is classified below the model, not just MCP-namespaced
ones. Three branches feed one conceptual choke-point:

- **MCP tools** (`mcp__<server>__<tool>`) — the PreToolUse safeguard hook and
  external-scope hook run the scope + safeguards gates. External-MCP lookups are
  **server-qualified** (`{server}.{tool}` then bare fallback), so two servers
  exposing the same bare tool name classify independently.
- **Built-in SDK tools / text-mode dispatch** — SDK-native calls resolve
  `builtin.<wire-name>` and text calls resolve `bus.<wire-name>` against
  `PolicyDataset.tool_targets`. A qualified entry defines how policy handles the
  call; it does not expose the capability to the model. `ExtractorSpec(none=True)`
  is an explicit targetless classification, not a blanket exemption for unknown
  tools. Unknown SDK names remain unclassified and fail policy closed.
- **Provider-runtime tools** — see "The provider gate" below.

`builtin_tools` and the derived SDK `allowed_tools` list control capability
exposure and headless permission prompts only; neither authorizes policy. Rollout
is **staged**: shadow mode records `builtin_policy_shadow` denials but permits
dispatch, while `enforce_builtin_policy: true` makes the same denial effective.
The deprecated `PolicyDataset.trusted_builtins` field is accepted only as a
bounded shadow-migration aid: an unclassified tool must also be actually
SDK-enabled, and reliance emits one structured `legacy_trusted_builtin` warning
per runner/tool. Enforce mode ignores the field completely. Universal safeguards
and narrower read-containment, subagent-approval, and approval hooks remain able
to deny independently in both modes.

`daemon/_policy_hook_adapter.py` normalizes both SDK and MCP hook payloads into
the same `ToolInvocation`, and carries a **replay cache** keyed on
`tool_use_id`: a reused id whose arguments differ is rejected rather than
served the earlier verdict.

### The provider gate

`_build_options` — where the Claude-SDK path registers safeguards,
`approve_before`, and the token-budget floor — runs only when the agent has no
provider runtime. Every provider runtime (codex, polybrain, anything registered
through the entry point) reaches tool handlers with no SDK hooks at all. Scope
survives, because it lives *inside* the built handler; safeguards, operator
consent, and the budget floor do not.

`runtime.gate_tool_bundle(bundle, *, agent_name, server, checks,
gate_budget_sec, bus_tool_names)` is the seam that puts them back. It wraps
every `AgentTool.handler` so the checks run first, and it lives in `runtime.py`
— not on the daemon mixin — so a host that doesn't compose `AgentRunnerFactory`
can still apply it. Four properties are load-bearing:

- **Denials raise.** `PolicyDenied` is a `PermissionError` (hence `OSError`), so
  a backend that discards handler results still fails closed, and the codex MCP
  gateway's existing catch renders it as `isError: True` rather than a
  successful result whose text happens to say "denied".
- **Names are qualified the way the dataset is keyed.** Factory tools synthesize
  `mcp__<server>__<tool>`; bus tools synthesize `mcp__bus__<agent>__<tool>`,
  because `mcp_identity` canonicalizes those to `bus.<name>`. Passing bare names
  would take the built-in branch and double-evaluate scope — a gate that looks
  armed and classifies wrong.
- **`updatedInput` is threaded through.** An operator's edited command runs
  instead of the original.
- **Check order matters.** Budget gate first (cheapest deny; a parked agent must
  never reach an interactive prompt), safeguards next, `approve_before` last —
  it is the only check that can block on a human. `gate_budget_sec` publishes
  the human's share of the deadline as a tool annotation so a provider's own
  per-call timeout can't cancel a call while the operator is still reading.

`tests/test_provider_gate_parity.py` holds this as a property against the live
provider registry: each registered provider gets a real tool call driven through
it, asserting the handler never ran.

### The control ladder

Five rungs sit under the model, each independently enforceable:

| Rung | Mechanism | Where |
|---|---|---|
| **CAPABILITY** | which tools exist on an agent's surface at all | `builtin_tools`, tool/bundle builder |
| **ACTION** | scope + safeguards on every call, all transports | `policy/`, PreToolUse hooks, `gate_tool_bundle` |
| **DELEGATION** | typed bus, cycle detection, operator-approved reach | `bus/_delegation.py`, `coord/delegation_graph.py` |
| **BUDGET** | token ceilings enforced by warn / park / interrupt | `daemon/_budget.py` |
| **STOP** | provable quiescence — the agent is *demonstrably* dead | `runner.quiesce`, `proc_registry`, `cgroups` |

**BUDGET.** `BudgetLedger` is append-only and keyed by `(agent, epoch,
turn_seq)`; `spent` is always derived by summing entries, never a mutable
counter. Because it is keyed on the runner's per-incarnation epoch and lives on
the *daemon*, a runner `reset` — which destroys and recreates the runner under a
fresh epoch — cannot zero it. That is the anti-laundering property. The daemon
does the accounting and returns a verdict *level* (`ok` / `warn` / `over`); the
runner owns the enforcement *action* (park vs interrupt), because only it can
interrupt the live turn. Ceilings are configured per agent under
`token_budgets.<agent>` with an optional shared pool at `token_budgets.__pool__`;
the most restrictive verdict wins. Accounting is exact, but usage arrives on
`TurnCompletedEvent`, so up to one completed turn of overshoot past the ceiling
is unavoidable — there is deliberately no grace buffer to hide it.

**STOP.** A runner is an in-process `asyncio` task with no OS presence, so
"prove the runner is dead" reduces to "prove the subprocesses its tools spawned
are dead". `runner.quiesce()` runs a bounded ladder — await the task for `grace`,
then `cancel()` and await `force` more, then reap any registered survivor — and
returns a `QuiescenceReport` carrying the evidence: whether the task is done,
the backend child's pid state (`proven_dead` / `alive` / `no_pid`), tool pids
reaped vs survived, and the cgroup state. Tier 1 is `proc_registry`, a per-runner
pid registry owned by a `ContextVar` bound at the top of each runner task; a
capability-wrapped tool spawned `start_new_session=True` is `killpg`-ed, an
unwrapped one is a single-pid `kill` (a group signal there would hit the daemon
itself). Tier 2 is `cgroups`: when the daemon runs in a writable, delegated
cgroup v2 subtree, each runner gets `<D>/agents/<runner>` and the killswitch
reaps the whole subtree with one `cgroup.kill` write — which survives
`fork` + `setsid` + reparenting. When neither tier can prove emptiness the report
says `unverified`. It never says "quiescent" on faith.

### Resource scope and the authorization snapshot

Beyond hosts and networks, scope rules can name **repository, cloud, and SaaS**
resources. `policy/resource_identity.py` parses one tagged authoring string
(`repo:`, `cloud:`, `saas:`) into its sole canonical representation, so two
spellings of the same resource cannot both exist as distinct rules.

`policy/_authorization_snapshot.py` freezes the whole authorization state —
rules, research policy, session posture, resource context, credential bindings —
into a content-addressed `AuthorizationSnapshot`. `snapshot_id` is the SHA-256
of a canonically-serialized payload that includes a `SNAPSHOT_IDENTITIES` block
pinning the API, canonicalization, extractor, policy, and schema versions, so a
snapshot written by an incompatible kernel is rejected on read rather than
silently reinterpreted. Snapshots chain by `predecessor_id`, giving generation
ordering and checkpoint rollback.

### Reconstruct — one turn, one causal chain

Every turn carries a `correlation_id` (`<engagement>:<agent>:<epoch>:<seq>`).
`coord/reconstruct.build_reconstruction(context, scope, correlation_id)` joins
every correlation-keyed record for that turn — jobs, assembled-prompt SHA,
operator questions, trust bypasses, scope/safeguard denies, remote calls, usage
and cost — into one time-ordered chain.

The anti-green-paint invariant is that the best-effort `audit_mirror` copy of a
deny must be a subset of the authoritative, fail-closed `scope.db` rows. That is
checked as a **multiset difference over `(agent, tool)`**, not a count
comparison — equal counts say nothing about equal contents, so a dropped row
paired with a spurious one would otherwise cancel out and read as a clean chain.
The result reports what it actually checked (`coverage`, `cross_check`) rather
than a bare boolean, flags legacy pre-`agent`-keyed ids as `ambiguous` (they can
name several unrelated turns, so `complete` can never be True for one), and
counts shadow-mode denies separately — those are a permissive outcome the
operator should see, not a gap.

### `_launch_profile` — per-agent privilege separation

The daemon injects an agent's `launch:` block from `agents.yaml` into
`factory_config` under the opaque key `_launch_profile`
(`daemon/_runner_factory.py`). The kernel **never interprets it** — the tool
builder (skin-side) resolves it to a capability-scoped subprocess launcher.
Absent `launch:` ⇒ the key is not injected ⇒ unprivileged default. It mirrors
the existing daemon-injection convention for `_posture` / `_scope_networks` /
`_authed_sessions` / `worker_hub`.

## Data flow

```
Operator message
    ↓
Daemon.prompt(agent, message)
    ↓
AgentRunner.submit(message) → queue           [operator-prompt admission:
                                               hard_refuse mode screens the
                                               prompt before dispatch]
    ↓
AgentRunner loop:
    1. Load context (ContextStore); mint the turn's correlation_id
    2. Drive the AgentBackend (Claude SDK by default; codex / polybrain /
       a registered provider otherwise) with the system prompt + tools
    3. The backend calls tools:
       a. Bus tools (ask_agent, kg_assert, record_review, …)
       b. Tool-builder tools (downstream-provided; run in a
          privilege-separated subprocess when a `_launch_profile` is present)
    4. Every tool call is classified below the model (see
       "Tool-authorization boundary"): MCP-namespaced tools through the
       PreToolUse hooks; SDK-native and text tools through explicit qualified
       `PolicyDataset.tool_targets` classifications; provider-runtime tools
       through `gate_tool_bundle`
    5. Cross-agent delegations land in the operator's QuestionInbox
    6. Response text → evidence capture → KG updates
    7. TurnCompletedEvent → budget_charge → verdict → park or interrupt
    ↓
Job.result.text → caller
```

## Persistence

All state is SQLite. The kernel takes a path rather than hardcoding a filename;
the names below are the conventional ones used throughout the code and tests.

| Store | File | Tables |
|---|---|---|
| `ContextStore` | `state.db` | `context` (methodology notes), `meta` (KV), `events`, `jobs`, `prompt_versions`, `questions`, `approval_bypass`, `audit_mirror`, `remote_calls`, `usage_ledger` |
| `KnowledgeGraph` | `kg.db` | `kg_facts` — noisy-OR corroboration, embeddings, TTL, provenance |
| `ScopeStore` | `scope.db` | scope rules, `scope_decisions`, `scope_snapshots`, `scope_head` |
| `ActionLedger` | per-engagement | `actions` — tool-call history (dir 0700 / file 0600; it keeps verbatim args, so it is protected at rest rather than content-redacted) |

`QuestionInbox` is in-memory with **write-through** to the `ContextStore`'s
`questions` table when a store is supplied, so pending operator questions
survive a daemon restart.

The turn-keyed tables (`jobs`, `questions`, `approval_bypass`, `audit_mirror`,
`remote_calls`, `usage_ledger`) all carry `correlation_id` — that is what makes
`coord/reconstruct.py` a join rather than a heuristic.

## Testing

1090 tests across 89 files (plus 23 subtests), 66% overall coverage
(`pytest tests/ --cov=salient_core`; the CI gate is an interim `fail_under = 30`).
Bus wire schemas are additionally pinned byte-for-byte by golden-master
snapshots (31 of them, `tests/golden/bus_schemas/`, see
`docs/BUS_TOOL_FIELDS.md`), and the remote-worker frame formats by
`tests/golden/worker_protocol/`.

Coverage is uneven by design, tracking how much of a module needs a live daemon
harness. Pure mechanism is well covered — reconstruct, `_budget`, scope
placeholders, the policy registry, `scope_api`, polybrain types and
`worker_protocol` are at or near 100%; KG 91%, `runtime` 99%,
`_policy_hook_adapter` 97%, `scope_evaluation` 93%. Integration-heavy modules
are thinner: `_questions` 26%, `_delegation` 31%, the KG/discovery/lifecycle bus
tool closures 25–32%, `_runner_factory` 51%, `runner` 61%, `scope` 69%.

One test is skipped unless the optional `[codex]` extra is installed
(`importorskip("openai_codex")`).
