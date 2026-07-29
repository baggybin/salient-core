# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in `salient-core`, please report it
responsibly:

1. **Do NOT open a public GitHub issue.**
2. Use GitHub's private vulnerability reporting: go to the repository's
   **Security** tab → **Report a vulnerability**
   (<https://github.com/baggybin/salient-core/security/advisories/new>).
   Include a description and, if possible, a proof of concept.
3. You will receive an acknowledgment within 48 hours.
4. A fix will be prioritized based on severity.

## Scope

`salient-core` is a coordination kernel — it provides inter-agent message
passing, policy gates, a knowledge graph, and a runner architecture. It does
not directly execute network operations, offensive tooling, or any
domain-specific work. Security vulnerabilities in the kernel's own
infrastructure (the bus, the scope gate, the safeguards engine, the runner,
the provider gate, the audit trail) are in scope. Domain-specific
vulnerabilities belong in the downstream application that uses the kernel.

Particularly interesting classes of report:

- **A path that reaches a tool handler without passing a gate** — a transport,
  provider runtime, or dispatch route the policy checks don't cover.
- **A scope-target extraction bypass** — an encoding or indirection that names
  a host/resource the extractor can't see. Note that `raw_argv` is documented
  as best-effort (see `docs/EXTRACTION.md`); a gap there is a known limitation
  of that extractor, not a bypass of a typed tool factory.
- **A control that reports success it can't back up** — a quiescence report
  claiming a dead process that lives, an audit chain reporting `complete` while
  a deny went unrecorded, budget spend that a restart or reset launders away.

## Design Principles

The kernel enforces a **default-deny** posture:

- Every tool invocation passes through scope + safeguards gates enforced
  *below* the model — across SDK built-ins, internal and external MCP,
  model-emitted text, and provider runtimes. Capability exposure and policy
  authorization are separate: enabling a tool never implicitly authorizes it,
  and an unclassified tool fails closed.
- A denied call **raises** rather than returning a denial, so a runtime that
  discards handler results still fails closed.
- Inter-agent delegation is typed and reach-limited, with cycle detection.
- Operator approval gates dangerous or cross-team operations, and an operator's
  *edited* command is what actually runs.
- Token ceilings are enforced against an append-only, epoch-keyed ledger that a
  runner reset cannot zero.
- Stop is *provable*: `quiesce()` reports the evidence, and reports
  `unverified` when it has none.
- Audit records are redacted structurally, and the best-effort mirror is
  cross-checked against the authoritative store so a dropped record surfaces as
  a gap rather than a clean history.
