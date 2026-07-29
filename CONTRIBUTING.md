# Contributing to salient-core

Thank you for your interest in contributing! This document covers the basics.

## This repo is a snapshot of the kernel

`salient-core` is the kernel; a private `salient` app is a downstream security
*skin* that depends on it. This repository is a curated **snapshot**: kernel
work lands upstream and is synced here as reviewable commits, so `src/` and
`tests/` here are meant to *be* the kernel rather than a re-derivation of it.
Its version tracks the upstream kernel's.

Keep the public API stable (guarded by `tests/test_public_api.py`) and expose
new capabilities through the Protocol contracts in `salient_core/protocols.py`,
the provider-neutral contracts in `salient_core/runtime.py`, and the runtime
`set_*` / `register_*` seams — rather than baking a consumer's domain specifics
into the kernel. Since the public release is paused and there are no external
consumers yet, breaking-but-additive kernel changes are cheap now — prefer
landing them before publish.

Two invariants are worth stating explicitly, because they are what the kernel is
*for*:

- **A new transport or runtime inherits the gates by construction**, never by
  remembering to wire them. If you add a path that reaches tool handlers, route
  it through the existing gate seam (`runtime.gate_tool_bundle` or the
  PreToolUse hooks) instead of mirroring the checks. Mirrors rot;
  `tests/test_provider_gate_parity.py` exists because one did.
- **Never report a control as stronger than it is.** When the kernel can't prove
  something — a subprocess died, a deny was recorded — it says so
  (`unverified`, `scope_gaps`, `no_pid`). Prefer an honest degraded answer to a
  clean-looking one.

## Development setup

```bash
git clone https://github.com/baggybin/salient-core.git
cd salient-core
pip install -e ".[dev]"
pre-commit install
```

## Code style

- **Python ≥3.11** — use modern syntax (`Self`, `LiteralString`, exception groups).
- **`ruff check` + `ruff format`** — the formatter runs in CI and pre-commit.
  No manual formatting needed.
- **`mypy`** — the configuration in `pyproject.toml` is the gate (relaxed from
  strict while extracted modules are annotated incrementally); it runs in CI
  and must pass. Type annotations are required for all public APIs; tests are
  exempt.
- **100-char line length** (advisory, enforced by formatter).

## Tests

```bash
pytest tests/ -q                          # fast unit tests (~11s, 1090 tests)
pytest tests/ --cov=salient_core          # with coverage (currently 66%; the
                                          # gate is an interim ≥30%, rising to
                                          # 80% as the kernel fills out)
```

One test is skipped unless the optional `[codex]` extra is installed.

Bus wire schemas are pinned byte-for-byte by golden masters. If you change a
bus tool's args model, regenerate and **review the diff** — a schema change
should be exactly the reviewable delta you intended:

```bash
UPDATE_BUS_GOLDENS=1 pytest tests/test_bus_schema_golden.py
```

See [`docs/BUS_TOOL_FIELDS.md`](docs/BUS_TOOL_FIELDS.md) for the field-typing
rubric before adding or changing a bus tool.

Every PR must pass the full CI gate: ruff (check **and** `format --check`),
mypy, pytest with coverage on Python 3.11 / 3.12 / 3.13.

## Commit style

Conventional commits preferred: `feat(bus):`, `fix(safeguards):`, `docs:`,
`refactor(scope):`, `test:`. Keep commits atomic; one logical change per
commit.

## Signing

Signed commits (GPG or SSH) are required for all contributions to the public
release.

## DCO

By submitting a pull request, you certify that you have the right to submit
the work under the Apache 2.0 license (Developer Certificate of Origin).
