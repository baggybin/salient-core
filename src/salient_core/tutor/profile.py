"""Learner-gradebook bucketing — a read-time view over ``learner_profile``.

``KnowledgeGraph.learner_profile(subject)`` returns the raw per-topic mastery
facts (predicate ``strong_topic`` / ``weak_topic`` / ``misconception``,
``confidence`` = mastery, ``review_due`` epoch). This helper buckets them the way
a study UI wants — due-for-review (most overdue first), strong, weak, and
misconceptions — and annotates each topic with its current recall odds computed
from the real forgetting curve (:func:`schedule.retrievability`), not an
approximation. Domain-general: any app with a ``learner:<id>`` subject can use it.
"""

from __future__ import annotations

import time
from typing import Any

from ..memory.kg import KnowledgeGraph
from . import schedule


def bucketed_profile(
    kg: KnowledgeGraph,
    subject: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Bucket a learner subject's gradebook into due/strong/weak/misconceptions.

    Each strong/weak topic carries ``{topic, mastery, review_due, ts, due,
    recall}`` where ``recall`` ∈ [0,1] (or None) is the estimated probability of
    recall right now. ``due`` lists strong+weak topics whose review is overdue,
    most overdue first.
    """
    now = now if now is not None else time.time()
    facts = kg.learner_profile(subject, now=now)

    def entry(f: dict[str, Any]) -> dict[str, Any]:
        review_due = f.get("review_due")
        return {
            "topic": f.get("object", ""),
            "mastery": float(f.get("confidence") or 0.0),
            "review_due": review_due,
            "ts": f.get("ts"),
            "due": bool(f.get("due")),
            "recall": schedule.retrievability(review_due, f.get("prev_interval_days"), now),
        }

    strong = [entry(f) for f in facts if f.get("predicate") == "strong_topic"]
    weak = [entry(f) for f in facts if f.get("predicate") == "weak_topic"]
    misconceptions = [
        {"topic": f.get("object", "")} for f in facts if f.get("predicate") == "misconception"
    ]
    due = sorted(
        (e for e in (*strong, *weak) if e["due"] and e["review_due"] is not None),
        key=lambda e: e["review_due"],
    )
    return {
        "due": due,
        "strong": sorted(strong, key=lambda e: -e["mastery"]),
        "weak": sorted(weak, key=lambda e: e["mastery"]),
        "misconceptions": misconceptions,
        "counts": {
            "strong": len(strong),
            "weak": len(weak),
            "due": len(due),
            "misconceptions": len(misconceptions),
        },
    }
