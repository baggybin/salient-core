"""T3.1 spine — reconstruct a turn's causal chain from its correlation id.

Stitches every correlation-keyed record for one turn into a single time-ordered
view, and cross-checks the best-effort ``audit_mirror`` scope denies against the
authoritative, fail-closed ``scope.db`` rows so a silently-dropped deny surfaces
as a GAP instead of a clean-looking chain (the anti-green-paint invariant from
the PR2 design: ``mirror ⊆ scope.db`` by correlation_id).

That subset relation is checked as a **multiset difference over
``(agent, tool)``** — see ``_deny_key`` for why no stronger key is available,
and ``CROSS_CHECK`` for the strength reported to callers. It is deliberately
not a cardinality comparison: equal counts do not mean equal contents, so a
drop paired with a spurious row would otherwise cancel out and read complete.

Completeness is honestly scoped: it covers the SCOPE + SAFEGUARD deny surfaces
that ride the PreToolUse hook. Killswitch / budget-park / authz-latch block tools
elsewhere and are NOT claimed here.

Pure over its two stores (a ContextStore for state.db, a ScopeStore for scope.db)
so it needs no daemon and is directly testable.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

COVERAGE = "scope+safeguard"

# What the mirror↔scope.db cross-check actually compares. Reported alongside
# `complete` so a consumer is never left to assume the check was stronger than
# it is — the same honesty rule the killswitch proof fields follow.
CROSS_CHECK = "agent+tool multiset"

# Mirror ops that satisfy an authoritative scope.db deny row. `scope_deny` is an
# ENFORCED deny; `scope_deny_shadowed` is one that scope evaluated but the hook
# let through (unclassified built-in, shadow mode). Both must count, because
# scope.db records `verdict='deny'` either way — counting only the enforced one
# reports a permitted call as a LOST DENIAL RECORD, which is both a false alarm
# on every shadowed call and backwards (it makes control look stricter than it
# was). The distinction is preserved in the chain, not erased.
_SCOPE_DENY_OPS = ("scope_deny", "scope_deny_shadowed")


def is_legacy_correlation_id(correlation_id: str) -> bool:
    """True for a pre-2026-07-25 id: ``<engagement>:<epoch>:<seq>``.

    Those were minted from a PROCESS-LOCAL epoch, so a daemon restart re-issued
    ids that already existed on disk — one id can name several unrelated turns,
    even turns belonging to different agents. Current ids carry the agent
    (``<engagement>:<agent>:<epoch>:<seq>``), so a 3-part id with a numeric
    middle part is old and cannot be trusted to identify one turn. We flag those
    rather than rewriting recorded history.
    """
    parts = correlation_id.split(":")
    return len(parts) == 3 and parts[1].isdigit()


def _deny_key(row: Any) -> tuple[Any, Any]:
    """Join key for the mirror↔scope.db cross-check: ``(agent, tool)``.

    Both sides record these from the same PreToolUse hook invocation, so they
    are identical by construction. Nothing else here is safe to join on:

    * ``ts`` — the two tables each stamp their own ``time.time()``, so the
      values never match exactly.
    * ``tool_use_id`` — the mirror's idempotency key, but ``scope_decisions``
      has no such column, so there is no shared unique id.
    * ``reason`` — the mirror stores ``evaluation.reason`` while scope.db
      stores the recorded ``result.summary``, and some paths wrap it (e.g.
      ``f"extractor refused: {check.summary}"``), so the two can differ for the
      same deny.

    Keying on a field that merely *usually* matches would turn every healthy
    chain into a permanent false INCOMPLETE — the same green-paint failure in
    the opposite direction, and a louder one, because an alarm that is always
    on teaches the operator to ignore it.
    """
    return (row.get("agent"), row.get("tool"))


def build_reconstruction(context: Any, scope: Any, correlation_id: str) -> dict[str, Any]:
    """Return the reconstructed chain for ``correlation_id`` (see module doc)."""
    jobs = context.load_jobs_for_correlation(correlation_id)
    questions = context.load_questions_for_correlation(correlation_id)
    bypasses = context.load_bypasses_for_correlation(correlation_id)
    audit = context.load_audit_mirror(correlation_id)
    remote_calls = context.load_remote_calls(correlation_id)
    usage = context.load_usage(correlation_id)
    authoritative = scope.scope_denies_for_correlation(correlation_id)

    # ── anti-green-paint cross-check (multiset difference over _deny_key) ──
    # scope.db is the authoritative, fail-closed home for scope denies; the
    # audit_mirror copy is best-effort. A scope.db deny with no mirror row ⇒ the
    # mirror silently dropped it ⇒ INCOMPLETE, and we surface the authoritative
    # rows so the deny is visible. A mirror row with no scope.db backing is a
    # hard-fail flag (a deny that never happened authoritatively).
    #
    # This compares CONTENT, not cardinality. `len(auth) == len(mirror)` says
    # nothing about whether the two sides describe the same denies: one dropped
    # row plus one spurious row cancel out under a count check and read as a
    # clean chain. A multiset difference catches that, and — unlike slicing
    # `authoritative[:missing]` — it names the denies that are genuinely
    # unmirrored instead of whichever rows happened to sort first.
    mirror_scope_denies = [r for r in audit if r.get("op") in _SCOPE_DENY_OPS]
    shadowed_denies = sum(1 for r in audit if r.get("op") == "scope_deny_shadowed")
    auth_counts = Counter(_deny_key(r) for r in authoritative)
    mirror_counts = Counter(_deny_key(r) for r in mirror_scope_denies)
    missing_by_key = auth_counts - mirror_counts  # authoritative, absent from mirror
    orphan_by_key = mirror_counts - auth_counts  # mirror, no authoritative row
    missing = sum(missing_by_key.values())
    orphan = sum(orphan_by_key.values())
    # Walk the authoritative rows in order, taking only as many per key as that
    # key is short in the mirror. Rows sharing a key are interchangeable (the
    # key is all we can match on), so any representative is honest; rows whose
    # key IS fully mirrored are never flagged.
    remaining = dict(missing_by_key)
    scope_gaps: list[dict[str, Any]] = []
    for row in authoritative:
        key = _deny_key(row)
        if remaining.get(key):
            remaining[key] -= 1
            scope_gaps.append({**row, "source": "authoritative", "mirror": "missing"})

    # A legacy id may name SEVERAL unrelated turns (see
    # `is_legacy_correlation_id`), so nothing assembled under it can be claimed
    # as one turn's chain. `complete` is about record integrity, and integrity
    # is unknowable when the key itself is ambiguous — so it can never be True
    # for a legacy id, however clean the cross-check looks.
    ambiguous = is_legacy_correlation_id(correlation_id)
    complete = missing == 0 and orphan == 0 and not ambiguous

    usage_totals = {
        "input_tokens": sum(int(u.get("input_tokens") or 0) for u in usage),
        "output_tokens": sum(int(u.get("output_tokens") or 0) for u in usage),
        "cache_read_tokens": sum(int(u.get("cache_read_tokens") or 0) for u in usage),
        "cache_create_tokens": sum(int(u.get("cache_create_tokens") or 0) for u in usage),
        "cost_usd": sum(float(u.get("cost_usd") or 0.0) for u in usage),
        "turns": len(usage),
    }

    # ── unified time-ordered chain ─────────────────────────────────────
    chain: list[dict[str, Any]] = []
    for j in jobs:
        chain.append(
            {
                "ts": j.get("submitted_at"),
                "seq": 0,
                "kind": "job",
                "agent": j.get("agent"),
                "summary": (j.get("prompt") or "")[:120],
            }
        )
    for q in questions:
        chain.append(
            {
                "ts": q.get("asked_at"),
                "seq": 0,
                "kind": "question",
                "agent": q.get("agent"),
                "summary": (q.get("text") or "")[:120],
                "answered": q.get("answered_at") is not None,
            }
        )
    for b in bypasses:
        chain.append(
            {
                "ts": b.get("ts"),
                "seq": 0,
                "kind": "trust_bypass",
                "agent": b.get("caller"),
                "summary": f"{b.get('gate')} → {b.get('target')} ({b.get('trust_scope')})",
            }
        )
    for a in audit:
        # Shadowed rows say so in the summary as well as the kind: the whole
        # point is that the operator sees the tool RAN, even on a renderer that
        # only prints summaries.
        shadow_note = (
            " [SHADOWED — policy in shadow mode, tool RAN]"
            if (a.get("op") == "scope_deny_shadowed")
            else ""
        )
        chain.append(
            {
                "ts": a.get("ts"),
                "seq": a.get("seq") or 0,
                "kind": a.get("op"),
                "agent": a.get("agent"),
                "summary": f"{a.get('tool')}: {a.get('reason') or ''}"[:120] + shadow_note,
            }
        )
    for rc in remote_calls:
        chain.append(
            {
                "ts": rc.get("ts"),
                "seq": 0,
                "kind": "remote_call",
                "agent": rc.get("agent"),
                "summary": f"{rc.get('tool')} @ {rc.get('session_id')} → {rc.get('outcome')}",
            }
        )
    for g in scope_gaps:
        chain.append(
            {
                "ts": g.get("ts"),
                "seq": 0,
                "kind": "scope_deny",
                "agent": g.get("agent"),
                "summary": f"{g.get('tool')}: {g.get('reason') or ''} "
                f"[source=authoritative, mirror=missing]"[:160],
            }
        )
    chain.sort(key=lambda e: (e["ts"] if e["ts"] is not None else 0.0, e["seq"]))

    found = bool(jobs or questions or bypasses or audit or remote_calls or usage or authoritative)
    return {
        "correlation_id": correlation_id,
        "found": found,
        "complete": complete,
        # True when the id itself is pre-fix and may name several unrelated
        # turns — surfaces must warn instead of presenting one clean chain.
        "ambiguous": ambiguous,
        "legacy_id": ambiguous,
        # Scope evaluated a deny, shadow mode let the call through. NOT a gap —
        # a permissive outcome the operator should see.
        "shadowed_denies": shadowed_denies,
        "coverage": COVERAGE,
        "cross_check": CROSS_CHECK,
        "jobs": jobs,
        "questions": questions,
        "bypasses": bypasses,
        "denies": audit,
        "scope_gaps": scope_gaps,
        "orphan_mirror_denies": orphan,
        "remote_calls": remote_calls,
        "usage": usage,
        "usage_totals": usage_totals,
        "chain": chain,
    }
