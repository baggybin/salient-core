"""Bus knowledge-graph tools — kg_assert, kg_query, kg_neighbors.

Triple-store assertions + queries for cross-agent fact sharing.
Extracted from salient/bus.py during the package split.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ..memory.embeddings import get_embedder
from ..memory.kg import _DEFAULT_TTL_DAYS
from ..tutor.schedule import (
    GRADES,
    next_interval_days,
    next_mastery,
    normalize_grade,
    predicate_for,
)
from ._common import *  # noqa: F401,F403
from ._common import bus_tool

if TYPE_CHECKING:
    from ..protocols import DaemonServices


# ── Post-write hook seam ─────────────────────────────────────────────
# A downstream skin may register a callback fired AFTER a successful kg_assert
# write (and after any contradiction flag), receiving
# ``(daemon, owner, fact, confidence, contradicts)``. The kernel does nothing
# with it; a skin uses it to trigger e.g. background verification of a
# low-confidence or contradicting claim. Best-effort: the write already
# succeeded, so a hook error is swallowed and never surfaces as a kg_assert
# failure. Default None (no hook); same injection idiom as the other seams.
_kg_assert_hook: Callable[..., None] | None = None


def set_kg_assert_hook(hook: Callable[..., None]) -> None:
    """Register a post-``kg_assert``-write callback. Called once at startup by a
    downstream skin; receives ``(daemon, owner, fact, confidence, contradicts)``."""
    global _kg_assert_hook
    _kg_assert_hook = hook


# Wire schemas. Notable choices (all shape-faithful in commit 1; de-require +
# constraints follow):
#   * kg_assert.ttl_days is float|None: absence (None) triggers the engagement-
#     default TTL, while an explicit <=0 means "never expires" — three distinct
#     states no bare-numeric default can encode. NO ge= (0/negative are meaningful).
#   * record_review.grade reproduces its enum from the imported GRADES constant
#     via json_schema_extra (NOT a hardcoded Literal — the values are owned by the
#     scheduler, and the handler's normalize_grade stays the single, case-lenient
#     validator). pydantic doesn't enforce json_schema_extra, matching the old
#     SDK-advertised-but-handler-validated contract exactly.
#   * min_score is an unbounded threshold (>1 ⇒ match nothing, <0 ⇒ match all —
#     both meaningful): a downstream clamp bounds the score, not a threshold vs it.
class _KgAssertArgs(BaseModel):
    subject: str
    predicate: str
    object: str
    # [0,1] domain: the KG consumer (_noisy_or) clamps confidence to [0,1], so
    # ge=/le= enforces the domain it always had. No 0.0 quirk here (the handler
    # used `is not None`, not a falsy `or`), so an explicit 0.0 already worked.
    confidence: float = Field(
        1.0, ge=0, le=1, description="0..1 confidence in the fact; defaults to 1.0."
    )
    # allow_inf_nan=False: this field writes a persisted expiry, and a NaN would
    # slip through `ttl_days > 0` (always False for NaN) to silently mean "never
    # expires" — reject inf/NaN at validation instead. The domain is otherwise
    # unbounded on purpose (0/negative are meaningful sentinels for "never").
    ttl_days: float | None = Field(
        None,
        allow_inf_nan=False,
        description="days until the fact expires; 0/negative = never; omit for the "
        "engagement default (~30 days).",
    )
    permanent: bool = False
    contradicts: str = ""


class _KgQueryArgs(BaseModel):
    subject: str = ""
    predicate: str = ""
    object: str = ""
    limit: int = Field(20, ge=1, description="max rows to return; defaults to 20.")


class _KgNeighborsArgs(BaseModel):
    entity: str
    depth: int = Field(1, ge=1, description="hops out from the entity; defaults to 1.")
    limit: int = Field(50, ge=1, description="max neighbors to return; defaults to 50.")


class _KgStatsArgs(BaseModel):
    pass


class _KgSemanticQueryArgs(BaseModel):
    text: str
    top_k: int = Field(10, ge=1, description="max results to return; defaults to 10.")
    # Unbounded on purpose: a threshold is compared against the (clamped) cosine
    # scores, so >1 (match nothing) and <0 (match all) are meaningful, not garbage.
    # But allow_inf_nan=False: a NaN threshold makes every `score >= NaN` False,
    # silently matching NOTHING — a garbage input better rejected than mystifying
    # the caller with an empty result (matches the ttl_days inf/NaN guard). Not a
    # schema keyword, so the golden is unchanged.
    min_score: float = Field(
        0.5,
        allow_inf_nan=False,
        description="cosine similarity threshold; results scoring below it are excluded. "
        "Use a value above 1 to match nothing, below 0 to match all. Defaults to 0.5.",
    )
    subject_prefix: str = ""


class _RecordReviewArgs(BaseModel):
    topic: str
    grade: str = Field(json_schema_extra={"enum": list(GRADES)})


# The tutor's single-learner namespace (matches prompts/tutor.md). record_review
# scopes every gradebook write to this subject.
_LEARNER_SUBJECT = "learner:op"


def _default_ttl_days(daemon: DaemonServices) -> float:
    """Engagement-profile override for the default fact TTL, else the
    module default. `kg.default_ttl_days: 0` (or negative) disables the
    default — engagement facts then persist until explicitly expired."""
    kg_block = (getattr(daemon, "profile", None) or {}).get("kg") or {}
    val = kg_block.get("default_ttl_days")
    try:
        return float(val) if val is not None else float(_DEFAULT_TTL_DAYS)
    except (TypeError, ValueError):
        return float(_DEFAULT_TTL_DAYS)


def _flag_contradiction(
    daemon: DaemonServices,
    agent: str,
    fact: Any,
    contradicts: str,
    eng_id: str | None,
) -> None:
    """Surface an agent-flagged KG contradiction to the operator on BOTH the
    live daemon event feed (EventHub → /ws/events/all) and the durable Q-inbox
    (an operator note). Best-effort by design: a missing/failed notice must
    never break the kg_assert write that already succeeded, so every step is
    guarded and a notice on one channel still fires if the other raises."""
    note = (
        f"KG contradiction flagged by {agent}: "
        f"({fact.subject}) -[{fact.predicate}]-> ({fact.object}) "
        f"conflicts with {contradicts!r}"
    )
    hub = getattr(daemon, "event_hub", None)
    if hub is not None:
        try:
            hub.publish(
                {
                    "event": "kg_contradiction",
                    "agent": agent,
                    "engagement_id": eng_id,
                    "fact": {
                        "fact_id": fact.id,
                        "subject": fact.subject,
                        "predicate": fact.predicate,
                        "object": fact.object,
                    },
                    "contradicts": contradicts,
                    "text": note,
                    "ts": time.time(),
                }
            )
        except Exception:  # noqa: BLE001 — notice must never break the write
            pass
    inbox = getattr(daemon, "inbox", None)
    if inbox is not None:
        try:
            q = inbox.add_note(from_op=f"agent:{agent}", text=note)
            inbox.publish("new", q)
        except Exception:  # noqa: BLE001 — same: best-effort
            pass


def make_kg_tools(daemon: DaemonServices, owner: str) -> list:
    """Returns [kg_assert, kg_query, kg_neighbors, kg_stats,
    kg_semantic_query, record_review] in _BUS_TOOL_NAMES order."""

    @bus_tool(
        "kg_assert",
        "Record a fact in the long-lived knowledge graph (cross-engagement "
        "memory). Triple shape: (subject) -[predicate]-> (object). Use "
        "namespaced subjects/objects so different fact types don't collide "
        "(e.g. 'host:10.0.0.1', 'service:name/port', 'doc:report-id', "
        "'user:name@host'). Common predicates: has_service, version, "
        "related_to, provides, works_against, discovered_by, "
        "investigated.\n"
        "  subject    — REQUIRED.\n"
        "  predicate  — REQUIRED.\n"
        "  object     — REQUIRED.\n"
        "  confidence — Optional. 0.0-1.0.\n"
        "  ttl_days   — Optional. Days until this fact expires and stops "
        "showing up in queries (0 or negative = never). Omit to take the "
        "default: engagement-scoped facts expire after ~30 days, so the KG "
        "trims one-off observations on its own. Re-asserting the same triple "
        "refreshes the clock.\n"
        "  permanent  — Optional. true = never expires (durable cross-"
        "engagement knowledge, e.g. a library→version fact). Overrides ttl_days.\n"
        "  contradicts — Optional. Note/value this assertion CONFLICTS with "
        "(e.g. a previously-asserted object like 'host:10.0.0.1:23'). Flags "
        "the fact + notifies the operator — use it when your finding "
        "disagrees with something already in the KG. Independent agents "
        "asserting the SAME triple instead corroborate it (confidence rises, "
        "shown as [corroborated ×N]).",
        _KgAssertArgs,
    )
    async def kg_assert(args: dict[str, Any]) -> dict[str, Any]:
        s = args["subject"].strip()
        p = args["predicate"].strip()
        o = args["object"].strip()
        if not s or not p or not o:
            return _text("error: subject, predicate, object all required", error=True)
        conf_f = args["confidence"]  # model-validated float in [0,1]
        eng_id = None
        if daemon.engagement_path is not None:
            eng_id = daemon.engagement_path.name
        # Expiry policy (the store stores an absolute epoch; this wrapper owns
        # the day→epoch conversion + the engagement default).
        #   permanent:true            → never expires
        #   ttl_days given (>0)       → now + ttl_days
        #   ttl_days <= 0             → never expires
        #   else, engagement-scoped   → now + default_ttl_days
        #   else (no engagement)      → never expires
        now = time.time()
        # Model-validated: ttl_days is float|None (None ⇒ engagement default;
        # explicit <=0 ⇒ never expires; >0 ⇒ that many days).
        ttl_days = args["ttl_days"]
        if args["permanent"]:
            expires_at: float | None = None
        elif ttl_days is not None:
            expires_at = (now + ttl_days * 86400) if ttl_days > 0 else None
        elif eng_id is not None:
            default_ttl = _default_ttl_days(daemon)
            expires_at = (now + default_ttl * 86400) if default_ttl > 0 else None
        else:
            expires_at = None
        contradicts = args["contradicts"].strip() or None
        try:
            fact = daemon.kg.assert_fact(
                s,
                p,
                o,
                confidence=conf_f,
                agent=owner,
                engagement_id=eng_id,
                expires_at=expires_at,
                contradicts=contradicts,
            )
        except Exception as e:  # noqa: BLE001
            return _text(f"kg_assert error: {type(e).__name__}: {e}", error=True)
        # Agent explicitly flagged a conflict → notify the operator (live feed
        # + durable Q-inbox). Best-effort: never let a notice failure surface
        # as a kg_assert error (the write already succeeded).
        if contradicts:
            _flag_contradiction(daemon, owner, fact, contradicts, eng_id)
        # Post-write skin hook (e.g. background verification of a low-confidence
        # claim). Best-effort — the write already succeeded and must never be
        # broken by a hook error.
        if _kg_assert_hook is not None:
            try:
                _kg_assert_hook(daemon, owner, fact, conf_f, contradicts)
            except Exception:  # noqa: BLE001 — best-effort; never break the write
                pass
        if expires_at is None:
            return _text(f"recorded (permanent): {fact}")
        days = (expires_at - now) / 86400
        return _text(f"recorded (expires in ~{days:.0f}d): {fact}")

    @bus_tool(
        "kg_query",
        "Pattern-match the knowledge graph. Substring matches (case-"
        "insensitive). ALWAYS check the KG before doing fresh research — "
        "we may already have learned a fact in a prior engagement. "
        "Returns up to `limit` matching facts ordered by recency. The "
        "handler refuses calls that omit ALL three filters (would dump "
        "everything), so pass at least one of subject / predicate / "
        "object.\n"
        "  subject   — Optional. Substring on the fact's subject.\n"
        "  predicate — Optional. Substring on the predicate.\n"
        "  object    — Optional. Substring on the object.\n"
        "  limit     — Optional. Max rows.",
        _KgQueryArgs,
    )
    async def kg_query(args: dict[str, Any]) -> dict[str, Any]:
        s = args["subject"].strip() or None
        p = args["predicate"].strip() or None
        o = args["object"].strip() or None
        limit = args["limit"]  # model-validated int, ge=1
        if not (s or p or o):
            return _text(
                "error: provide at least one of subject/predicate/object "
                "to filter — kg_query with no filters would dump everything",
                error=True,
            )
        try:
            facts = daemon.kg.query(s, p, o, limit=limit)
        except Exception as e:  # noqa: BLE001
            return _text(f"kg_query error: {type(e).__name__}: {e}", error=True)
        if not facts:
            return _text("(no matching facts)")
        return _text("\n".join(str(f) for f in facts))

    @bus_tool(
        "kg_neighbors",
        "Walk the knowledge graph from `entity`, returning all facts "
        "where it appears (depth=1) or facts within `depth` hops. Useful "
        "for 'tell me everything we know that touches host X' or "
        "'what's connected to this entity'. Capped at `limit` total facts.",
        _KgNeighborsArgs,
    )
    async def kg_neighbors(args: dict[str, Any]) -> dict[str, Any]:
        ent = args["entity"].strip()
        if not ent:
            return _text("error: 'entity' is required", error=True)
        depth = args["depth"]  # model-validated int, ge=1
        limit = args["limit"]  # model-validated int, ge=1
        try:
            facts = daemon.kg.neighbors(ent, depth=depth, limit=limit)
        except Exception as e:  # noqa: BLE001
            return _text(f"kg_neighbors error: {type(e).__name__}: {e}", error=True)
        if not facts:
            return _text(f"(no facts touching {ent!r})")
        return _text("\n".join(str(f) for f in facts))

    @bus_tool(
        "kg_stats",
        "Summarize the knowledge graph: how many active (non-expired) facts "
        "it holds, distinct entities, engagements covered, the top "
        "predicates, and how many facts expire within the next 7 days. Use "
        "it to gauge how much prior knowledge exists before a deep dive, or "
        "to spot facts about to age out. No params.",
        _KgStatsArgs,
    )
    async def kg_stats(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
        try:
            st = daemon.kg.stats()
        except Exception as e:  # noqa: BLE001
            return _text(f"kg_stats error: {type(e).__name__}: {e}", error=True)
        top = (
            ", ".join(f"{tp['predicate']}×{tp['count']}" for tp in st.get("top_predicates", []))
            or "(none)"
        )
        lines = [
            f"KG: {st['total_facts']} active facts, "
            f"{st['distinct_entities']} entities, "
            f"{st['engagements']} engagements.",
            f"  expiring within 7 days: {st['expiring_within_7d']}",
            f"  top predicates: {top}",
        ]
        return _text("\n".join(lines))

    @bus_tool(
        "kg_semantic_query",
        "Search the knowledge graph by MEANING (embedding similarity), not "
        "substrings — finds relevant prior facts even when they don't share "
        "exact words with your query. Use it for 'what do we know related to "
        "<situation>'. Returns up to `top_k` facts ranked by relevance. Falls "
        "back to a note if semantic search isn't configured for this engagement "
        "(then use kg_query for substring matching).\n"
        "  text          — REQUIRED. Natural-language description to match against.\n"
        "  top_k         — Optional. Max facts.\n"
        "  min_score     — Optional. cosine floor.\n"
        "  subject_prefix — Optional. Restrict the ranked set to facts whose "
        "subject starts with this string (e.g. 'study:<id>:' to search ONE "
        "study project and nothing else). Omit to search the whole graph.",
        _KgSemanticQueryArgs,
    )
    async def kg_semantic_query(args: dict[str, Any]) -> dict[str, Any]:
        text = args["text"].strip()
        if not text:
            return _text("error: 'text' is required", error=True)
        embedder = get_embedder(getattr(daemon, "profile", None))
        if embedder is None:
            return _text(
                "semantic search is not configured for this engagement; use "
                "kg_query for substring matching instead",
                error=True,
            )
        top_k = args["top_k"]  # model-validated int, ge=1
        min_score = args["min_score"]  # model-validated float (unbounded threshold)
        subject_prefix = args["subject_prefix"].strip() or None
        try:
            qv = await embedder.embed_one(text)
            if qv is None:
                return _text(
                    "semantic search unavailable (embedder did not respond); use kg_query instead",
                    error=True,
                )
            hits = daemon.kg.semantic_query(
                qv,
                model=embedder.model,
                top_k=top_k,
                min_score=min_score,
                subject_prefix=subject_prefix,
            )
        except Exception as e:  # noqa: BLE001
            return _text(f"kg_semantic_query error: {type(e).__name__}: {e}", error=True)
        if not hits:
            return _text("(no semantically relevant facts)")
        return _text("\n".join(f"[{score:.2f}] {fact}" for fact, score in hits))

    @bus_tool(
        "record_review",
        "Record a graded drill/quiz outcome for the operator and get back WHEN "
        "to review the topic next. Use this for drill results INSTEAD of "
        "kg_assert: it runs a spaced-repetition scheduler so the next-review "
        "interval EXPANDS on a clean recall and resets on a lapse, and it can "
        "LOWER mastery when recall slips (kg_assert can only ever raise it). "
        "The learner gradebook lives under subject 'learner:op'.\n"
        "  topic — REQUIRED. The concept/technique just drilled (e.g. "
        "'binary search', 'graph traversal'). Keep the wording STABLE across "
        "sessions so repeated reviews of the same skill chain into one "
        "schedule.\n"
        "  grade — REQUIRED. How recall went: 'again' = failed/blanked (a "
        "lapse), 'hard' = recalled with effort, 'good' = clean recall, 'easy' "
        "= trivial. Grade the RETRIEVAL, not the lesson.",
        _RecordReviewArgs,
    )
    async def record_review(args: dict[str, Any]) -> dict[str, Any]:
        topic = (args.get("topic") or "").strip()
        if not topic:
            return _text("error: 'topic' is required", error=True)
        try:
            grade = normalize_grade(args.get("grade") or "")
        except ValueError as e:
            return _text(f"record_review error: {e}", error=True)
        now = time.time()
        try:
            state = daemon.kg.learner_review_state(_LEARNER_SUBJECT, topic)
            prev_interval = state["prev_interval_days"] if state else None
            prev_mastery = state["mastery"] if state else None
            interval = next_interval_days(prev_interval, grade)
            mastery = next_mastery(prev_mastery, grade)
            predicate = predicate_for(mastery)
            daemon.kg.record_learner_review(
                _LEARNER_SUBJECT,
                topic,
                predicate=predicate,
                mastery=mastery,
                review_due=now + interval * 86400,
                agent=owner,
                now=now,
            )
        except Exception as e:  # noqa: BLE001
            return _text(f"record_review error: {type(e).__name__}: {e}", error=True)
        tier = "strong" if predicate == "strong_topic" else "weak"
        return _text(
            f"recorded: {topic!r} graded {grade} → mastery {mastery:.2f} "
            f"({tier}); next review in ~{interval:.0f}d"
        )

    return [kg_assert, kg_query, kg_neighbors, kg_stats, kg_semantic_query, record_review]
