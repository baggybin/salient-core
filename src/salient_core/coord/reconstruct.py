"""T3.1 spine — reconstruct a turn's causal chain from its correlation id.

Stitches every correlation-keyed record for one turn into a single time-ordered
view, and cross-checks the best-effort ``audit_mirror`` scope denies against the
authoritative, fail-closed ``scope.db`` rows so a silently-dropped deny surfaces
as a GAP instead of a clean-looking chain (the anti-green-paint invariant from
the PR2 design: ``mirror ⊆ scope.db`` by correlation_id).

That subset relation is checked **structurally** when the data allows it: every
scope.db deny carries a ``decision_id`` (== the mirror's ``tool_use_id``), so the
two sides match row-for-row on that id and a same-``(agent, tool)`` drop+spurious
pair can no longer cancel (two tool calls carry two ids). Pre-migration denies
have a NULL ``decision_id`` and fall back to a **multiset difference over
``(agent, tool)``** — the historical, weaker check, with its same-key blind spot
— and the tier actually used is disclosed to callers via ``cross_check``
(``"decision_id"`` vs ``"agent+tool multiset"``). See the cross-check body and
``_decision_key`` / ``_deny_key``.

Completeness (the ``complete`` verdict + the cross-check) is honestly scoped to
the SCOPE + SAFEGUARD deny surfaces that ride the PreToolUse hook. The other
floors that stop a tool ARE surfaced in the chain — they just don't enter the
deny cross-check: budget-park / authz-latch as best-effort mirror ``blocks``
(H2), and a killswitch STOP via a point-in-turn time-window join (H2, matched on
agent + the turn's [start, end] span) — so a parked or STOPped turn no longer
reconstructs as a clean chain that just ends.

Pure over its two stores (a ContextStore for state.db, a ScopeStore for scope.db)
so it needs no daemon and is directly testable.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

COVERAGE = "scope+safeguard"

# What the mirror↔scope.db cross-check actually compared, reported alongside
# `complete` so a consumer is never left to assume the check was stronger than
# it was — the same honesty rule the killswitch proof fields follow. STRONG is a
# per-row id match (structural); the multiset is the weaker legacy fallback.
CROSS_CHECK_STRONG = "decision_id"
CROSS_CHECK = "agent+tool multiset"

# Mirror ops that satisfy an authoritative scope.db deny row. `scope_deny` is an
# ENFORCED deny; `scope_deny_shadowed` is one that scope evaluated but the hook
# let through (unclassified built-in, shadow mode). Both must count, because
# scope.db records `verdict='deny'` either way — counting only the enforced one
# reports a permitted call as a LOST DENIAL RECORD, which is both a false alarm
# on every shadowed call and backwards (it makes control look stricter than it
# was). The distinction is preserved in the chain, not erased.
_SCOPE_DENY_OPS = ("scope_deny", "scope_deny_shadowed")

# T3.1 H2 — block ops: a tool stopped by a floor that is NOT a scope/safeguard
# deny (`budget_park`, `authz_latch`; killswitch STOP is time-window-joined, not
# a mirror row). Surfaced in the chain and in a structured `blocks` list, but
# DELIBERATELY excluded from `_SCOPE_DENY_OPS` so they never enter the
# scope↔mirror cross-check — a block is a real, permitted-to-record event, not a
# dropped deny, and must not fabricate a `missing`/`orphan`.
_BLOCK_MIRROR_OPS = ("budget_park", "authz_latch")


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
    * ``reason`` — the mirror stores ``evaluation.reason`` while scope.db
      stores the recorded ``result.summary``, and some paths wrap it (e.g.
      ``f"extractor refused: {check.summary}"``), so the two can differ for the
      same deny.

    Keying on a field that merely *usually* matches would turn every healthy
    chain into a permanent false INCOMPLETE — the same green-paint failure in
    the opposite direction, and a louder one, because an alarm that is always
    on teaches the operator to ignore it.

    Since T3.1 H1 there IS a shared unique id — ``scope_decisions.decision_id``
    == the mirror's ``tool_use_id`` (see ``_decision_key``) — used for the strong
    structural check. This ``(agent, tool)`` key is the weaker fallback for the
    pre-migration denies that predate that column.
    """
    return (row.get("agent"), row.get("tool"))


def _decision_key(row: Any) -> Any:
    """Strong per-row twin key on the authoritative side: ``decision_id``, the id
    the audit_mirror copy carries as ``tool_use_id``. Used when every scope.db
    deny in the chain has one — matching row-for-row on it closes the
    same-``(agent, tool)`` blind spot of ``_deny_key`` (two tool calls have two
    ids, so a drop and a spurious row can never share a key and cancel)."""
    return row.get("decision_id")


def build_reconstruction(context: Any, scope: Any, correlation_id: str) -> dict[str, Any]:
    """Return the reconstructed chain for ``correlation_id`` (see module doc)."""
    jobs = context.load_jobs_for_correlation(correlation_id)
    questions = context.load_questions_for_correlation(correlation_id)
    bypasses = context.load_bypasses_for_correlation(correlation_id)
    audit = context.load_audit_mirror(correlation_id)
    remote_calls = context.load_remote_calls(correlation_id)
    usage = context.load_usage(correlation_id)
    authoritative = scope.scope_denies_for_correlation(correlation_id)

    # Whether ANY correlation-keyed record exists for this id. Computed up here
    # so `complete` can gate on it (see below) — a chain that does not exist is
    # not "complete", it is absent.
    found = bool(jobs or questions or bypasses or audit or remote_calls or usage or authoritative)

    # ── killswitch STOP (time-window join, H2) ────────────────────────────
    # STOP is proc-level — an agent + a time, not a request id — so it can't be
    # correlation-keyed. Point-in-turn join: a STOP dispatched within this turn's
    # [start, end] span (for the turn's agent) cut the turn short. Without it a
    # STOPped turn reconstructs as a chain that just ends. The agent comes from
    # the turn's job(s); the span from every record's timestamp (a terminated
    # job's finished_at extends the window to the kill).
    turn_agent = jobs[0].get("agent") if jobs else None
    _span_ts = [
        t
        for t in (
            [j.get("submitted_at") for j in jobs]
            + [j.get("finished_at") for j in jobs]
            + [q.get("asked_at") for q in questions]
            + [b.get("ts") for b in bypasses]
            + [a.get("ts") for a in audit]
            + [rc.get("ts") for rc in remote_calls]
        )
        if t is not None
    ]
    stop_events: list[dict[str, Any]] = []
    if turn_agent and _span_ts:
        stop_events = context.load_stop_events_in_span(turn_agent, min(_span_ts), max(_span_ts))

    # ── anti-green-paint cross-check ──────────────────────────────────────
    # scope.db is the authoritative, fail-closed home for scope denies; the
    # audit_mirror copy is best-effort. A scope.db deny with no mirror row ⇒ the
    # mirror silently dropped it ⇒ INCOMPLETE, and we surface the authoritative
    # rows so the deny is visible. A mirror row with no scope.db backing is a
    # hard-fail flag (a deny that never happened authoritatively).
    #
    # STRONG (structural) check — used when every authoritative deny carries the
    # per-row twin key `decision_id` (== the mirror's `tool_use_id`): match
    # denies row-for-row on that id. Two tool calls carry two ids, so a
    # same-`(agent, tool)` drop+spurious pair no longer cancels — the exact
    # failure a count/multiset check misses (one dropped mirror write for call
    # t1, plus a fail-closed audit-persist deny for call t2 that mirrors with no
    # scope.db row). Under id-matching those surface as `missing=t1` + `orphan=t2`
    # ⇒ INCOMPLETE.
    #
    # WEAK (legacy) fallback — pre-migration denies have a NULL `decision_id` and
    # cannot be id-joined, so the whole chain drops to the `(agent, tool)`
    # multiset (the historical check, with its same-key blind spot) and DISCLOSES
    # the weaker tier via `cross_check`, never silently passing a weak check off
    # as the strong one. It is decided per-reconstruction: a turn's rows are all
    # one era, so id-bearing and legacy rows never mix inside one verdict.
    mirror_scope_denies = [r for r in audit if r.get("op") in _SCOPE_DENY_OPS]
    shadowed_denies = sum(1 for r in audit if r.get("op") == "scope_deny_shadowed")
    # Block-mirror rows (budget-park / authz-latch) — a structured view of the
    # non-deny floors that stopped a tool this turn. Already in the chain via the
    # generic op loop below; surfaced here so a consumer can find them without
    # re-filtering the raw audit dump.
    blocks = [r for r in audit if r.get("op") in _BLOCK_MIRROR_OPS]

    strong = bool(authoritative) and all(r.get("decision_id") for r in authoritative)
    if strong:
        cross_check = CROSS_CHECK_STRONG
        keyer = _decision_key
        auth_counts = Counter(_decision_key(r) for r in authoritative)
        mirror_counts = Counter(r.get("tool_use_id") for r in mirror_scope_denies)
    else:
        cross_check = CROSS_CHECK
        keyer = _deny_key
        auth_counts = Counter(_deny_key(r) for r in authoritative)
        mirror_counts = Counter(_deny_key(r) for r in mirror_scope_denies)
    missing_by_key = auth_counts - mirror_counts  # authoritative, absent from mirror
    orphan_by_key = mirror_counts - auth_counts  # mirror, no authoritative row
    missing = sum(missing_by_key.values())
    orphan = sum(orphan_by_key.values())
    # Walk the authoritative rows in order, taking only as many per key as that
    # key is short in the mirror. Under the strong check the key is unique per
    # row, so this names the exact dropped deny; under the weak fallback rows
    # sharing a key are interchangeable, so any representative is honest.
    remaining = dict(missing_by_key)
    scope_gaps: list[dict[str, Any]] = []
    for row in authoritative:
        key = keyer(row)
        if remaining.get(key):
            remaining[key] -= 1
            scope_gaps.append({**row, "source": "authoritative", "mirror": "missing"})

    # A legacy id may name SEVERAL unrelated turns (see
    # `is_legacy_correlation_id`), so nothing assembled under it can be claimed
    # as one turn's chain. `complete` is about record integrity, and integrity
    # is unknowable when the key itself is ambiguous — so it can never be True
    # for a legacy id, however clean the cross-check looks.
    ambiguous = is_legacy_correlation_id(correlation_id)
    # `found and …`: an id with no records is absent, not complete. "Nothing
    # missing over zero rows" is vacuously true and semantically wrong — you
    # cannot assert a chain is whole when there is no chain. Both renderers gate
    # on `found` first, but the raw dict is the contract, so a consumer reading
    # `complete` directly (a scorer, an RPC caller, the cross-check job) must not
    # get a green for a request that does not exist.
    complete = found and missing == 0 and orphan == 0 and not ambiguous

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
    for s in stop_events:
        chain.append(
            {
                "ts": s.get("dispatched_at"),
                "seq": 0,
                "kind": "killswitch",
                "agent": s.get("agent"),
                "summary": (
                    f"emergency STOP dispatched ({s.get('state') or 'unverified'})"
                    + (f" — {s.get('reason')}" if s.get("reason") else "")
                ),
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
        "cross_check": cross_check,
        "jobs": jobs,
        "questions": questions,
        "bypasses": bypasses,
        "denies": audit,
        "blocks": blocks,
        "block_kinds": sorted({b.get("op") for b in blocks if b.get("op")}),
        "scope_gaps": scope_gaps,
        "orphan_mirror_denies": orphan,
        "remote_calls": remote_calls,
        "stop_events": stop_events,
        "usage": usage,
        "usage_totals": usage_totals,
        "chain": chain,
    }
