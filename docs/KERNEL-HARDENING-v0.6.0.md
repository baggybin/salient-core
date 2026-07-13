# Kernel Hardening — v0.6.0 (engineering log)

A record of the hardening arc that produced **v0.6.0**: how it started, the
7-finding review it closed, the review methodology, what the external reviewers
caught, the release, and the downstream fallout. The source review and the
rollout checklist it references are internal documents; this log summarizes
everything an adopter needs from them.

---

## How it started

A prior automated planning run (GPT/Codex) produced a kernel audit — an
internal invariant review that confirmed **7 defects**
— and a large plan, `policy-boundary-convergence`, to fix the biggest one
(finding #1, the tool-call authorization boundary). That run **completed all 7
implementation todos but stalled entering its own final verification wave
(F1–F5)** and produced no verdicts. The work sat as an uncommitted working-tree
diff (10 modified + 6 new source + 11 test files).

This effort:
1. **finished the stalled F-wave** for finding #1 and shipped it, then
2. **systematically closed the remaining 6 findings**, each on its own branch/PR.

## The 7 findings → what shipped

| # | Sev | Defect | Fix | PR |
|---|-----|--------|-----|----|
| 1 | High | Built-in & text-mode tools bypassed the policy boundary (allow keyed on the `mcp__` naming convention) | One transport-neutral authorization boundary (SDK / internal-MCP / external-MCP / text); capability exposure ≠ authorization; raw-vs-redacted dual audit snapshot; deny-before-side-effect; `enforce_builtin_policy` shadow/enforce rollout | #42 |
| 2 | High | A failed `ContextStore` commit left a dirty transaction a later commit could flush → cache/disk divergence | `_txn()` context manager rolls back on any failure (`BaseException`); commit-then-publish; connection invalidated if rollback itself fails; `busy_timeout` | #45 |
| 3 | High | Compaction archived one snapshot then `purge_expired()` re-selected → deleted unarchived rows; partial cross-store apply | Delete exactly the archived id set with a **delete-time expiry re-check** (`purge_expired_ids`); cross-store GC reported as an idempotent degraded phase, not claimed atomic | #46 |
| 4 | Med | `ExtractorSpec.fields` was a mutable dict → registered policy mutable after `set_active()` | `__post_init__` freezes `MappingProxyType(dict(fields))` (copy decouples, proxy blocks mutation) | #43 |
| 5 | Med | `ask_agent`'s non-detached child-stop was fire-and-forget, never joined | Await the stop (bounded, shielded) on ordinary timeout; park+join at teardown when the parent is cancelled; `track_background` / `join_background_tasks` seam | #47 |
| 6 | Med | Degraded health incomplete (migrations/prune swallowed silently) & not attributed | Every swallowed sink marks degraded; `health()` carries per-sink counts; dedicated `_health_lock` | #48 |
| 7 | Med | Startup SQLite `_peek_*` readers leaked connection handles | Wrapped in `contextlib.closing()` | #43 |

Plus, surfaced during downstream triage:

| — | — | Contract-violating `runner.submit()` crashed with opaque `NoneType.id` | Seam guard raises an actionable `TypeError` naming the target | #49 |

Also merged in the same window (adjacent kernel work, not part of the review):
KG transactional writes + snapshot readers (#39), `set_kg_builder` seam (#40),
subject-prefix scoping (#44), `Fact.source_ref` provenance (#36).

## Finding #1 — the stalled F-wave, finished

The `policy-boundary-convergence` plan's final wave (F1 plan-compliance, F2
code-quality, F3 runtime-QA, F4 scope-fidelity, F5 global-review) had never run.
Completing it:

- **Gates re-run from scratch:** ruff · mypy (62 files) · full suite (**616
  passed** at that point) · asyncio-debug + `ResourceWarning`-as-error · coverage.
- **Independent review (Fable + Gemini)** found **4 real edge-case defects** in
  the new code, all fixed with regression tests:
  1. an unconfigured scope store fabricated a "refusing fail-closed" audit record
     while still dispatching → clean-allow with an honest `scope_not_configured`;
  2. the credential redaction marker keyed off the **model-emitted** wire name (a
     text call could suppress its own audit) → keyed off the canonical identity;
  3. missing secret field names (`authorization`, `access_token`, `x-api-key`, …);
  4. non-str top-level keys weren't normalized across the raw/redacted snapshots.
- **Two fast-follows** (requested): fail-closed on a *broken* scope store; a
  validating `_scope_store` property so a wrong-typed store fails loudly.

Verdict: **APPROVE**, shipped as PR #42.

## The methodology (applied to every finding)

Each finding followed the same loop, and it earned its cost repeatedly:

```
scope/design-check with the reasoners (where warranted)
  → implement
  → dual review (Fable + GPT SOL, sometimes Gemini/GLM)
  → fix what they caught
  → gates (ruff · mypy · format · full pytest · asyncio-debug)
  → PR (+ posted review summary)
  → merge
```

- **Design-first on the hard ones.** Finding #5 (async cancellation semantics in
  a `finally`) went to a **3/3 council** (Fable + GPT SOL + GLM, strong
  consensus) *before* any code. Findings #2, #3, #6 got a design/approach check
  with Fable before implementing.
- **Every fix has a regression test verified to fail on the pre-fix code** — not
  just pass after. Where the review's prescribed test didn't reproduce (e.g. #7:
  CPython 3.12 doesn't emit `ResourceWarning` for unclosed sqlite connections),
  the test was rewritten to assert the invariant *directly* (connection is
  closed) rather than via a symptom that doesn't fire.
- **Scope discipline.** Findings were kept to their boundary; the preserved
  source review doc was never touched; larger ideas the
  reviewers raised (e.g. a cross-subsystem health aggregator, a durable
  compaction manifest) were explicitly evaluated and **declined with a reason**
  rather than scope-crept.

## What the reviews caught (the value)

The dual review was not a rubber stamp — it caught a real, shippable-if-missed
bug on most findings:

- **#2** — GPT SOL caught that if the failure-path `rollback()` *itself* failed,
  silently reusing the connection would **recreate the original bug**. Fix:
  invalidate + close the connection on rollback failure.
- **#3** — GPT SOL caught that switching to delete-by-id **lost the delete-time
  expiry re-check**, so a fact *revived* between archive and delete would be
  wrongly deleted — a regression introduced by the first commit. Fix: conditional
  `purge_expired_ids` (delete by id **and** re-check expiry/predicate).
- **#5** — the council designed the cancellation discrimination
  (`current_task().cancelling()`), then the implementation review caught an
  **unbounded straggler `gather`** in teardown (a task suppressing cancellation
  would hang shutdown forever). Fix: bounded wait.
- **#6** — Fable flagged that if any migration relied on catching a duplicate-column
  error (bare `ALTER`), marking degraded would cause a **false `ok:false` on every
  existing-DB startup**. Verified safe (all migrations are `table_info`-guarded).
  Also flagged a counter race → dedicated `_health_lock`.
- **#4/#7** — both reviewers confirmed `MappingProxyType` breaks
  pickle/deepcopy/`asdict`; verified moot (nothing serializes `ExtractorSpec`, and
  the sibling `PolicyDataset` already uses the identical idiom).

Test count over the arc: **616 → 659**.

## The release

- **v0.6.0** tagged and released (private repo). Annotated tag + GitHub release
  summarize the 7 findings + KG work.
- **Version reconciliation:** pyproject was bumped inside feature commits
  (`0.4.0→0.5.0` at #39, `0.5.0→0.6.0` at #44), so **0.5.0 was never tagged** —
  v0.6.0 is the first release since v0.4.0 and supersedes the never-released
  0.5.0. (Earlier working notes called it "0.5.0"; the tag matches pyproject.)
- **Default behavior is unchanged:** the policy boundary runs in **shadow**
  (`enforce_builtin_policy=False`) until a consumer opts in. `SCOPE_API_VERSION`
  stays `1`; `scope.gate()` / `scope_api` unchanged.

## Downstream fallout (and how it was diagnosed)

Cutting the release surfaced a real pin mismatch. A downstream application (and
a variant deployment of it) had an **editable install of the local salient-core
checkout (0.6.0)** while pinning `salient-core @ v0.4.0` — so the app suite ran
against a kernel newer than its test doubles and behavior assertions were written
for. This looked, from the downstream session, like a mystery kernel crash
(`_delegation.py:839  NoneType.id`).

Root-caused to **two layers**, neither from the downstream feature work:

1. **Stale test doubles (crash-class — fixed downstream):** `_FakeRunner.submit()`
   returned `None` with no `id` (kernel #41 tightened `submit() → Job(.id)`); the
   safeguard-hook fake lacked `_policy_dataset` / `_safeguard_config` (added by the
   policy-boundary refactor). Fixing the doubles took the failing set **49 → 12**.
   The kernel also gained the #49 seam guard so this fails with an actionable
   message next time.
2. **Pre-refactor behavior assertions (behavior-class — handed off):** 12 tests
   assert the pre-#42 safeguard/scope verdict + event shapes. High-confidence
   test-updates (salient-core's own `test_policy_*` / `test_hardening_invariants`
   are green on 0.6.0), not regressions — handed to the app session with a
   per-file diagnosis.

**Lesson:** an editable local-core install behind a stale version pin makes a
downstream suite silently track an unreleased, moving kernel. The fix is to bump
the pin to a tagged release (`v0.6.0`) and adopt via the staged shadow→enforce
rollout described in [`EXTRACTION.md`](EXTRACTION.md).

## Remaining / follow-ups

- **Downstream pin bumps** (`@v0.4.0` → `v0.6.0`) + the 12 behavior-test
  adaptations — owned by the downstream apps; sequence with the shadow→enforce
  rollout.
- **Adopt enforce mode** downstream once `tool_targets` coverage is complete
  (mine the shadow audit events first).
- The review's cross-subsystem **runtime health aggregator** (folding the daemon
  JSONL-log audit sinks into the status RPC) lives in the downstream
  daemon; the kernel `ContextStore.health()` surface it would consume is now
  complete.

## Reference — PRs

| PR | What |
|----|------|
| #42 | policy authorization boundary (finding #1 + F-wave) |
| #43 | `ExtractorSpec.fields` frozen (#4) + SQLite handle close (#7) |
| #45 | ContextStore commit rollback (#2) |
| #46 | compaction archive/delete equality + re-check (#3) |
| #47 | `ask_agent` child-stop structural join (#5) |
| #48 | ContextStore degraded-health completeness + attribution (#6) |
| #49 | `runner.submit()` seam guard |
| #50 | this release/adoption doc retarget to v0.6.0 |
