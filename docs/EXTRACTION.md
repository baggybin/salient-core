# Extending the Kernel

How a downstream application (the security skin, the tutor showcase, or
your own project) extends `salient-core`.

The kernel plugs into a downstream in two ways: **Protocol contracts** (typed
surfaces you implement) and **runtime registration seams** (`set_*` functions
you call at startup, each with a safe default). This guide covers the load-
bearing Protocols first, then the runtime seams.

## Protocol contracts

### 1. Implement `DaemonServices`

The runner back-injects its owning daemon and uses it only through the
`DaemonServices` Protocol. The full Protocol is broad — it declares the *whole*
surface the bundled bus tools reach, not just the runner's read-only slice — but
you only need the members whose features you actually use. The minimum for a
daemon that runs agents and serves bus tools:

```python
from pathlib import Path
from salient_core import ContextStore, KnowledgeGraph, QuestionInbox

class MyDaemon:
    # ── stores the bus tools read ──
    profile: dict = {}
    engagement_path: Path | None = None
    context: ContextStore
    kg: KnowledgeGraph
    inbox: QuestionInbox
    actions: Any          # ActionLedger
    runners: dict         # name → AgentRunner
    all_cfgs: dict        # name → agents.yaml config
    event_hub: Any        # EventHub

    def add_question(self, agent: str, question: str, job_id: int | None = None) -> int:
        return self.inbox.add(agent=agent, text=question, job_id=job_id)
```

See `salient_core/protocols.py` for the rest: the operator-approval question
methods (each returns a future the calling tool awaits), the in-flight
`bus_call_*` registry, redispatch accounting, `budget_charge`, and agent
lifecycle. Every member carries a comment naming its real shape.

### 2. Provide a `ToolBuilder` and/or a `ToolBundleBuilder`

There are two, because there are two kinds of runtime.

**`ToolBuilder`** builds an MCP *server* for the Claude-SDK path:

```python
def my_tool_builder(tool_type: str, config: dict, *, server_name: str | None = None):
    """Return (mcp_server, wire_name, builtin_tool_names)."""
    ...
```

**`ToolBundleBuilder`** builds a provider-neutral `ToolBundle` — plain
`AgentTool` handlers — for codex, polybrain, and any registered provider, which
have no SDK MCP server to attach to:

```python
from salient_core import AgentTool, ToolBundle
from salient_core.protocols import ToolBuildContext

def my_bundle_builder(tool_type, config, *, context: ToolBuildContext) -> ToolBundle:
    # context carries: server_name, scope_store, agent_name,
    #                  extra_tools, extra_bare_wires
    return ToolBundle((AgentTool(name=..., description=..., input_schema=..., handler=...),))
```

Register whichever your runtimes need; the default for both is a raising stub,
so a missed registration fails loudly at first agent start rather than producing
a silently tool-less daemon.

### 3. Provide an `AliasProtocol` (optional)

If you need custom tool-name mapping between the wire names a model sees and
the kernel's internal names, implement `AliasProtocol` and activate it:

```python
from salient_core import alias

class MyAlias:
    def to_wire(self, name: str) -> str: ...
    def to_real(self, name: str) -> str: ...
    def rewrite_outbound(self, text: str) -> str: ...
    def rewrite_inbound(self, text: str) -> str: ...
    def mapping(self) -> dict[str, str]: ...
    def enabled(self) -> bool: ...

alias.set_active(MyAlias())
```

If you don't need aliasing, the kernel default (`IdentityAlias`) passes
everything through unchanged. No action needed.

### 4. Wire the bus

Each agent gets its own bus MCP server:

```python
from salient_core import make_bus

bus_server, server_name, wire_names = make_bus(daemon, agent_name)
```

The bus captures the daemon reference in closures. The 31 bus tools (`ask_agent`,
`ask_agents`, `ask_partner`, `ask_consensus`, `ask_operator`, `kg_*`,
`record_review`, `context_*`, `list_agents`, `search_skills` / `get_skill`,
`propose_lesson` / `propose_skill`, `rule_validate` / `read_evidence` /
`prior_actions`, `spawn_template` / `swarm_finish`) are wired automatically. To
append domain tools, pass `extra_tools=` (a name collision with a built-in raises
rather than silently shadows), or register a wrapping builder via
`set_bus_builder` (below).

For a provider runtime, use `make_bus_tool_bundle(daemon, owner)` instead — it
returns `(ToolBundle, bare_wire_names)`. **Keep the bare wire names**: they are
what `gate_tool_bundle`'s `bus_tool_names` argument needs, and omitting a bus
tool there silently costs it every policy pattern keyed on `bus.*` — including
the delegation denylist.

### 5. Register an `AgentProvider` (optional)

Claude, Codex, and Polybrain are builtins. A third-party runtime implements
`AgentProvider` and either registers directly or ships an entry point under
`salient.agent_providers`:

```python
from salient_core import ProviderCapabilities, ProviderName, ProviderProbe

class MyProvider:
    name = ProviderName("my-runtime")
    capabilities = ProviderCapabilities(
        streaming=True, tools=True, interruption=True, context_usage=False,
    )

    async def probe(self) -> ProviderProbe:
        return ProviderProbe(available=True, detail="ok")

    def create_backend(self, config, *, tool_bundle=ToolBundle()) -> AgentBackend:
        ...
```

```toml
# pyproject.toml of the provider package
[project.entry-points."salient.agent_providers"]
my-runtime = "my_pkg.provider:MyProvider"
```

`AgentBackend` (in `salient_core.runtime`) is the runtime contract: `connect` /
`query` / `receive_response` / `interrupt` / `disconnect` /
`get_context_usage` / `diagnose_failure`, streaming the normalized `AgentEvent`
union. Implement the optional `ReapableBackend` extension (`child_pid` /
`child_alive`) if your backend owns a local model subprocess — that is what lets
`quiesce()` *prove* it died instead of reporting `no_pid`.

**Your provider inherits the policy gates by construction**, because every
provider tool bundle passes through `gate_tool_bundle`. There is nothing to opt
into, and `tests/test_provider_gate_parity.py` holds that property against the
live registry.

### 6. Apply the provider gate in your own host

If you compose your own daemon rather than the kernel's `AgentRunnerFactory`,
apply the gate yourself — this is the seam that puts safeguards, operator
consent, and the budget floor back on a path that has no SDK PreToolUse hooks:

```python
from salient_core.runtime import gate_tool_bundle

gated = gate_tool_bundle(
    bundle,
    agent_name=name,
    server=alias.to_wire(name),
    checks=[budget_gate, safeguard_hook, approve_before],  # order matters
    gate_budget_sec=approval_timeout if uses_approve_before else 0,
    bus_tool_names=bus_wires,
)
```

Order: cheap non-interactive checks first, so a call that will be refused anyway
never blocks on a human; `approve_before` last. A denied call raises
`PolicyDenied` (a `PermissionError`) from the wrapped handler rather than
returning a denial, so a backend that discards handler results still fails
closed.

## Runtime registration seams

Beyond the Protocols, the kernel exposes a family of `set_*` functions read at
call time (never bound at import). Each has a safe default, so you only call
the ones your skin needs; the kernel runs standalone otherwise.

```python
from salient_core.daemon import _tool_registry, _prompts, _questions, proc_registry
from salient_core.bus import _common, _delegation, _kg, set_bus_builder
from salient_core import alias
from salient_core.policy import registry as policy_registry

# Required to build real tools (defaults are fail-loud raising stubs):
_tool_registry.set_tool_builder(my_tool_builder)                 # SDK path
_tool_registry.set_tool_bundle_builder(my_bundle_builder)        # provider path
_tool_registry.set_tool_wire_names({"exec": ["run", "shell"]})   # advertised primary tools
_tool_registry.set_daemon_skin_modules(engagement=..., action_class=..., plugins=...)

# How daemon.kg is constructed (default builds the local SQLite store; swap in
# e.g. a network client with the same method surface):
_tool_registry.set_kg_builder(lambda db_path: RemoteKnowledgeGraph(url, token))

# Prompt assembly:
_prompts.set_thinking_provider(is_match, resolve)   # model-specific thinking config
_prompts.set_prompts_root("/path/to/prompt/addenda")

# Coordination hooks (all no-op by default):
_delegation.set_delegation_observer(my_observer_factory)
_delegation.set_agent_disabled_checker(lambda daemon, agent: ...)
_kg.set_kg_assert_hook(my_kg_hook)
_common.set_bus_skin_modules(credentials=my_cred_module)
_questions.set_authz_provider(my_authz_config_getter)

# Wrap the bus builder to inject domain tools on every agent's bus:
set_bus_builder(my_bus_builder)

# Tier-2 quiescence: give the kernel a cgroup reaper so a killswitch can prove
# the whole subtree died (default is the Tier-1 pid registry only):
proc_registry.set_cgroup_reaper(my_cgroup_reaper)

# Data + aliasing:
policy_registry.set_active(my_policy_dataset)   # scope targets, safeguard patterns
alias.set_active(MyAlias())                     # optional, default is IdentityAlias
```

### The policy dataset

SDK capability exposure and policy authorization are separate. `builtin_tools`
enables SDK capabilities; qualified `tool_targets` entries classify policy
handling without enabling anything in the SDK:

```python
from salient_core.policy import scope
from salient_core.policy.registry import PolicyDataset

my_policy_dataset = PolicyDataset(
    tool_targets={
        "builtin.Bash": scope.ExtractorSpec(fields={"command": "raw_argv"}),
        "builtin.Read": scope.ExtractorSpec(local_only=True),
        "builtin.Agent": scope.ExtractorSpec(none=True),
        "bus.context_write": scope.ExtractorSpec(none=True),
    },
    prohibited_patterns=...,
    loud_patterns=...,
    natural_language_prohibited=...,   # scanned by check_prompt_intent
    structural_transfer_tools=frozenset({"scp.copy"}),
    # Deprecated: temporary shadow-only migration input. Remove after every
    # enabled tool has a qualified tool_targets classification.
    trusted_builtins=frozenset({"LegacyKnownTool"}),
)
# TodoWrite, ExitPlanMode, WebSearch, and future SDK names stay absent until their
# schemas and intended policy handling are explicitly known.
```

The dataset is frozen on construction — mappings and sequences are canonicalized
into read-only structures, so holding a reference to the original input can't
mutate live policy afterwards. Swap the whole dataset via `set_active` instead.

Migrate one agent at a time:

1. Inventory its actual `builtin_tools` and auto-enabled `Agent`/`Task` tools.
2. Add a qualified `builtin.<name>` classification for every known schema; use
   `none=True` only for a deliberately targetless tool.
3. Run in shadow mode and resolve `builtin_policy_shadow` and
   `legacy_trusted_builtin` records. The legacy field never bypasses safeguards.
4. Remove the tool from `trusted_builtins`, then set
   `enforce_builtin_policy: true`. Enforce mode ignores legacy trust and denies
   any remaining unclassified call before dispatch.

### Additive vocabulary seams

Each `register_*` **extends** a generic built-in rather than replacing a
provider, so the kernel ships a working generic default and your skin layers its
specifics on top. Same call-time idiom, called once at startup.

```python
from salient_core.policy import scope, redaction
from salient_core.memory import credentials, kg
from salient_core.daemon import _prompts

scope.register_extractor("my_kind", my_extractor_fn)             # scope target extractor kind
credentials.register_credential_vocab({"ntlm": "has_ntlm_hash"}) # cred kind → KG predicate
redaction.register_secret_fields({"nt_hash", "aes256_key"})      # extra log-redaction field names
redaction.register_cred_tool_markers({"secretsdump"})            # tools whose value/hash/token hold secrets
kg.register_source_resolver("ticket", resolve_ticket)            # Fact.source_ref scheme
_prompts.register_swarm_bootstrap_addendum("domain swarm guidance …")
```

(`register_secret_fields` / `register_cred_tool_markers` live in
`policy.redaction`; they are also re-exported from `bus._common` for
compatibility.)

See the seam table in [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) for every seam,
its module, and its default.

### Writing a scope extractor

Import from `policy/scope_api.py`, not from the kernel's `_`-prefixed internals.
That facade is versioned, so the kernel can refactor freely as long as the
surface holds:

```python
from salient_core.policy.scope_api import (
    ExtractorCtx, ExtractionResult, Target,
    classify_token, register_extractor, require_scope_api_version, sweep_tokens,
)

# Hardcode the version YOUR extractors were written against — not
# SCOPE_API_VERSION, which would make the check a tautology. Kernel is at 3.
require_scope_api_version(3)   # startup assertion; raises ScopeApiVersionError

def extract_my_kind(ctx: ExtractorCtx) -> ExtractionResult: ...
register_extractor("my_kind", extract_my_kind)
```

An unknown extractor kind fails **closed** — it is a deny, not a pass-through.

### `raw_argv` is best-effort, not authoritative

The `raw_argv` extractor (used by `builtin.Bash` and every `*.run` shell escape)
runs a **static regex sweep** over free-form shell/Python to find IP/host/URL
targets, then scope-checks whatever it finds. It fails **closed** on the
obfuscation it recognizes — shell/process substitution, hex/integer/octal-encoded
IPs, unbound `$VARs`, a decode wrapper feeding a dynamic-exec sink
(`exec(base64.b64decode(...))`, `base64 -d | sh`), and addresses spliced across
adjacent string literals — and Unicode-normalizes (NFKC) before matching so
homoglyph digits can't slip an address past the sweep. It does **not** promise to
find every target: a runtime-computed string, a novel encoding, or multi-call
`/tmp` indirection can still name a host the sweep can't see. When the sweep finds
nothing the command is allowed (the contract is "if it names a target, that target
must be in scope" — `ls /tmp` legitimately names none), so treat `raw_argv` as one
layer of defense-in-depth behind the typed message bus and operator approval, not a
sealed boundary. Prefer routing target-bearing work through typed tool factories
(`nmap`, `ssh`, …) with explicit target fields, which get authoritative extraction.

Unresolved operator-infrastructure placeholders (`<lhost>`, `<lport>`,
`<rhost>`, `<rport>`) are rejected wherever they appear, including in targets a
registered extractor emits from a sibling arg. The scan total-covers every
runtime arg type and fails closed past a recursion depth cap.

## Token budgets

Ceilings live in the engagement profile under `token_budgets`. The kernel reads
them; your skin decides how they get there.

```yaml
token_budgets:
  __pool__:                  # optional shared engagement ceiling
    ceiling_tokens: 5_000_000
    warn_frac: 0.8
    on_exhaustion: pause     # warn | pause | stop
  researcher:
    ceiling_tokens: 800_000
    warn_frac: 0.9
    on_exhaustion: stop
    engagement_pool: true    # also counts against __pool__
```

The most restrictive verdict wins. `on_exhaustion: warn` is monitor-only and
never enforces. Your skin owns ledger durability: implement `_budget_load` /
`_budget_persist` so the snapshot round-trips to the engagement dir — otherwise a
daemon restart becomes another laundering path.

## Per-agent privilege separation (`_launch_profile`)

To isolate an agent's tool subprocess behind OS-level capability boundaries,
add a `launch:` block to that agent in `agents.yaml`. The daemon passes it
through opaquely as `factory_config["_launch_profile"]`; your tool builder
resolves it to a capability-scoped launcher:

```python
def my_tool_builder(tool_type, config, *, server_name=None):
    launch = config.get("_launch_profile")   # None ⇒ unprivileged default
    if launch:
        # spawn the tool subprocess under the requested capabilities
        ...
```

The kernel never interprets `_launch_profile` — all systemd/capability
mechanism lives skin-side. It mirrors the same injection convention as
`_posture`, `_scope_networks`, `_authed_sessions`, and `worker_hub`.

If your spawn funnel registers each child with
`daemon.proc_registry.register_subprocess(pid, wrapped=..., argv0=...)`, those
pids become part of the quiescence proof — that registry is the only thing that
populates Tier 1, and the runner and killswitch drain it.

## Shutdown contract

A downstream daemon MUST call `join_background_tasks()` during shutdown
**before it tears down agent backends**. `ask_agent`'s non-detached child-stop is
a tracked task that calls `runner.cancel_job` → `backend.interrupt()`, so joining
after backend teardown would drop it mid-interrupt.

```python
from salient_core.daemon import join_background_tasks

async def shutdown(self) -> None:
    await join_background_tasks(timeout=10.0)   # default timeout is 10.0
    # ...then tear down backends
```

## Minimal example

See [`salient-tutor`](https://github.com/baggybin/salient-tutor) for a
complete working example — a teaching agent that composes the kernel's
bus, KG, scheduler, and questions inbox into a Socratic coach.

## Data tables

The kernel ships with empty defaults for scope/safeguard data:

- `ScopeStore(targets={})` — no tool targets
- `SafeguardConfig(patterns={})` — no prohibited patterns

A downstream application populates these with domain-specific data at
startup. **Until it does, the gate refuses every tool call** — that is the
default-deny posture working as designed, not a misconfiguration.
