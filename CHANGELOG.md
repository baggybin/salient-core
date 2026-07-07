# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-06

### Added
- **Per-agent privilege separation seam**: the daemon injects an agent's
  `launch:` block from `agents.yaml` into `factory_config` under the opaque
  `_launch_profile` key (`daemon/_runner_factory.py`). The kernel never
  interprets it — a downstream tool builder resolves it to a capability-scoped
  subprocess launcher. Absent `launch:` ⇒ key not injected ⇒ unprivileged
  default. Mirrors the existing `_posture` / `_scope_networks` /
  `_authed_sessions` injection convention.

## [0.1.0] - 2026-07-06

Kernel convergence complete — mechanism + seams, single source of truth.
`salient-core` is now independently importable and testable, carrying only
generic coordination mechanism plus registration seams (zero references to
app-specific "skin" modules).

### Added
- **Runtime seam model** — call-time registration points (not import-time
  binds), each with a safe default, so a downstream skin plugs in at startup
  while the kernel stays runnable standalone: `set_bus_builder`
  (`bus/__init__.py`), `set_bus_skin_modules` (`bus/_common.py`),
  `set_kg_assert_hook` (`bus/_kg.py`), `set_delegation_observer` /
  `set_agent_disabled_checker` (`bus/_delegation.py`), `set_tool_builder` /
  `set_tool_wire_names` / `set_daemon_skin_modules` (`daemon/_tool_registry.py`),
  `set_thinking_provider` / `set_prompts_root` (`daemon/_prompts.py`),
  `set_authz_provider` (`daemon/_questions.py`), plus `alias.set_active` and
  `policy.registry.set_active`.
- **`@bus_tool` migration** — every bus tool family (`_context`, `_kg`,
  `_discovery`, `_credentials`, `_delegation`, `_lifecycle`, `_audit`, skills,
  lessons) now derives its wire schema from a Pydantic model and validates
  model args before the handler runs (`bus/_common.py`), with typed `BusFlags`
  (`bus/_flags.py`) carrying routing/write-back on the `.trusted` channel.
  Golden-master wire-schema snapshots (`tests/golden/bus_schemas/`) pin each
  schema byte-for-byte. Rubric documented in `docs/BUS_TOOL_FIELDS.md`.
- **Bus extensibility** — `make_bus` accepts an `extra_tools` slot so a skin can
  append domain tools; a name collision with a built-in raises rather than
  silently shadows.
- Kernel extraction from the upstream `salient` orchestrator: `bus/` (typed
  inter-agent tools incl. `ask_consensus` with semantic scoring, judge, and
  per-leg traces), `memory/` (noisy-OR knowledge graph, embeddings,
  `semantic_recall`), `coord/` (question inbox, delegation graph), `daemon/`
  (Claude-SDK agent runner behind the `DaemonServices` Protocol seam),
  `policy/` (scope + safeguards gates), `tutor/` (SM-2 scheduler +
  learner-gradebook bucketing), `protocols.py` seams, `alias.py` passthrough.
- Sealed public API: curated lazy exports at the top level (`__all__`,
  PEP 562), `py.typed` (PEP 561), `semantic_recall` / `bucketed_profile`
  convenience helpers.
- `examples/consensus_panel/` — split-pane consensus showcase (Starlette SSE
  server + offline mock runner scored by the kernel's real
  `semantic_agreement`).
- Public-release docs: `README.md`, `docs/ARCHITECTURE.md`,
  `docs/EXTRACTION.md`.
- Repository bootstrap: `pyproject.toml` (src layout, Apache 2.0, Python ≥3.11),
  `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.gitignore`,
  `.pre-commit-config.yaml`, CI workflow (ruff + mypy + pytest --cov).
- `PLAN.md` — comprehensive extraction + showcase plan (Path A).

### Fixed
- Public-surface contract fixes: `semantic_recall` never raises;
  `alias.__all__` matches the documented API; consensus judge honors
  `prefer_primary` and reports accurate skip reasons; per-leg consensus
  traces isolated by child job id; zero-norm vectors excluded from
  `semantic_agreement`.
- Prompt addenda moved into the package (`salient_core/prompts/`) so the
  runner factory finds them from a checkout and an installed wheel alike.

## [0.0.1] - 2026-06-30

### Added
- Empty package skeleton. No kernel modules yet — see `PLAN.md` for the
  extraction roadmap.
