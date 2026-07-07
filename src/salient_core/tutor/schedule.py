"""Deterministic spaced-repetition scheduler for the tutor's learner model.

The tutor records drill outcomes as learner-scoped knowledge-graph facts
(subject ``learner:op``). Until now every such fact carried a flat 30-day
TTL, so *expiry* — the proxy for the forgetting curve — was identical for a
skill drilled once and a skill drilled ten times. That is the crudest part
of the learner model: real retention follows an EXPANDING schedule (each
successful recall should push the next review further out; a lapse should
pull it back in).

This module is the deterministic core that fixes that. It is a pure
function library — no I/O, no clock, no KG — so it unit-tests exhaustively
and the daemon/bus layers stay thin wrappers (the bus tool feeds it the
prior interval + a grade and writes back what it returns). Keeping the
arithmetic OUT of the prompt is the point: an LLM eyeballing "how many days
until the next review" is exactly the weakness this replaces.

Model: a compact SM-2 / FSRS-lite. We deliberately do NOT persist a
per-fact ease factor (the KG triple store has no metadata column for it);
instead the *current interval is the state* — it lives in the existing
``expires_at - ts`` of the learner fact — and the next interval is a
grade-scaled multiple of it. That loses SM-2's per-item ease adaptation but
keeps the whole scheduler stateless and storable in the schema we already
have, which is the right trade at this scale.

Grades follow the familiar four-button recall scale:

    again — failed to recall (a lapse): interval resets, mastery drops
    hard  — recalled with difficulty: interval grows slowly
    good  — recalled cleanly: interval grows at the standard rate
    easy  — trivial recall: interval grows fastest

Mastery (the KG ``confidence``, 0.0-1.0) moves asymptotically toward 1.0 on
success and toward 0.0 on a lapse, so it never pins to a false certainty —
which also keeps it honest against the prompt's anti-absolutist rule.
"""

from __future__ import annotations

import math

# The four recall grades, weakest → strongest. Public so the bus tool can
# validate its argument and the prompt/tests can pin the vocabulary.
GRADES: tuple[str, ...] = ("again", "hard", "good", "easy")

# First-review interval (days) by grade — the schedule a brand-new fact
# (no prior interval) graduates onto. "again" on a first sight still earns
# a 1-day re-look, not zero, so it always re-surfaces.
_INITIAL_DAYS: dict[str, float] = {
    "again": 1.0,
    "hard": 1.0,
    "good": 1.0,
    "easy": 4.0,
}

# Multiplier applied to the PRIOR interval on a subsequent review. "again"
# ignores this and resets to the lapse floor. These are close to Anki's
# defaults (good ≈ ease 2.5 nudged down for a single-learner tutor; hard a
# gentle growth; easy a bonus on top of good).
_MULTIPLIER: dict[str, float] = {
    "hard": 1.2,
    "good": 2.3,
    "easy": 3.2,
}

# A lapse never throws the fact all the way back to "due now" (that would
# spam the warm-up queue); it returns to a short floor and re-graduates.
_LAPSE_FLOOR_DAYS = 1.0

# Guardrails: no review ever schedules more than a season out (learned
# material drifts; a year-long gap is as good as forgotten), and never
# less than a day (sub-day churn isn't spaced practice).
_MIN_DAYS = 1.0
_MAX_DAYS = 120.0

# Mastery defaults for a fact with no prior estimate — a cold topic starts
# uncertain, not at zero (the learner has at least been exposed to it).
_INITIAL_MASTERY = 0.3

# How far mastery moves toward its target on each grade. "good"/"easy"
# close the gap to 1.0; "again" pulls toward 0.0; "hard" nudges up slightly.
_MASTERY_GAIN: dict[str, float] = {
    "hard": 0.10,  # toward 1.0, small
    "good": 0.30,  # toward 1.0, standard
    "easy": 0.50,  # toward 1.0, large
}
_LAPSE_RETENTION = 0.5  # "again" multiplies mastery toward 0.0 by this

# Mastery at or above this reads as a "strong" topic; below, "weak". The
# bus tool uses it to pick which predicate the fact lands under, so a topic
# migrates strong↔weak as the estimate crosses the line.
STRONG_THRESHOLD = 0.6

# Read-time retrievability target: a fact is scheduled so its estimated recall
# probability has decayed to exactly _TARGET_R by its due date (the same 90%
# convention FSRS uses). _DECAY = ln(1/_TARGET_R) is the per-stability decay
# constant that makes that identity hold (see `retrievability`).
_TARGET_R = 0.9
_DECAY = -math.log(_TARGET_R)  # ≈ 0.10536


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def normalize_grade(grade: str) -> str:
    """Lower-cased, validated grade. Raises ValueError on anything not in
    GRADES so the bus tool fails loudly rather than silently mis-scheduling."""
    g = (grade or "").strip().lower()
    if g not in GRADES:
        raise ValueError(f"grade must be one of {GRADES}, got {grade!r}")
    return g


def next_interval_days(prev_days: float | None, grade: str) -> float:
    """Days until the next review, given the PRIOR interval and this grade.

    `prev_days` is the length of the interval just completed (``expires_at -
    ts`` of the existing fact, in days), or None/<=0 for a first review.
    The result is clamped to [_MIN_DAYS, _MAX_DAYS]."""
    g = normalize_grade(grade)
    if g == "again":
        return _LAPSE_FLOOR_DAYS
    if prev_days is None or prev_days <= 0:
        return _clamp(_INITIAL_DAYS[g], _MIN_DAYS, _MAX_DAYS)
    return _clamp(prev_days * _MULTIPLIER[g], _MIN_DAYS, _MAX_DAYS)


def next_mastery(prev_mastery: float | None, grade: str) -> float:
    """Updated mastery estimate (0.0-1.0) after a graded review.

    Success moves asymptotically toward 1.0 (so it never asserts a false
    certainty); a lapse multiplies toward 0.0. A cold fact starts at
    _INITIAL_MASTERY."""
    g = normalize_grade(grade)
    m = _INITIAL_MASTERY if prev_mastery is None else _clamp(float(prev_mastery), 0.0, 1.0)
    if g == "again":
        return _clamp(m * _LAPSE_RETENTION, 0.0, 1.0)
    gain = _MASTERY_GAIN[g]
    return _clamp(m + (1.0 - m) * gain, 0.0, 1.0)


def predicate_for(mastery: float) -> str:
    """Learner-fact predicate implied by a mastery estimate: ``strong_topic``
    at/above STRONG_THRESHOLD, else ``weak_topic``. Single source of truth so
    the bus tool and any reader agree on the strong/weak line."""
    return "strong_topic" if float(mastery) >= STRONG_THRESHOLD else "weak_topic"


def retrievability(
    review_due: float | None,
    prev_interval_days: float | None,
    now: float,
) -> float | None:
    """Estimated probability of recall R∈[0,1] for a scheduled learner fact at
    time ``now`` — a read-time view of the forgetting curve. Like the rest of
    this module it is pure (no clock, no KG); the caller passes the fact's
    stored ``review_due`` and the interval just scheduled, and gets back the
    current recall odds. It is NOT persisted: the KG triple store has no column
    for it, and it is cheaper to recompute on read than to keep fresh on write.

    Model (FSRS-lite): ``R = exp(-elapsed / S)`` where stability
    ``S = prev_interval_days / _DECAY`` is chosen so R hits _TARGET_R (0.9)
    exactly at the due date. The last review epoch is reconstructed from what
    the fact already stores — ``last_review = review_due - prev_interval`` — so
    ``elapsed = now - last_review = (now - review_due) + prev_interval``. Hence
    a just-reviewed fact → 1.0, a fact at its due date → 0.9, an overdue fact
    decays below 0.9.

    ``prev_interval_days`` is the interval just scheduled, derivable from the
    stored fact as ``(review_due - ts) / 86400`` (the same reconstruction
    ``learner_review_state`` does). Returns None when there is nothing to
    compute from (no ``review_due``, or a non-positive/None interval)."""
    if review_due is None:
        return None
    if prev_interval_days is None or prev_interval_days <= 0:
        return None
    elapsed_days = (now - review_due) / 86400.0 + prev_interval_days
    stability = prev_interval_days / _DECAY
    return _clamp(math.exp(-elapsed_days / stability), 0.0, 1.0)
