"""Deterministic, reversible data-store compaction.

This is the ONLY component that mutates the KG / context store for compaction.
The `kg_compactor` agent is a read-only ANALYST that surveys + proposes; this
engine APPLIES, and only the unambiguous, reversible, archive-first operations:

  - KG: archive then purge already-EXPIRED, NON-credential facts. Expired facts
    are already hidden from every query (the `_ACTIVE_CLAUSE`), so removing them
    can't change any answer. Credential-predicate facts and permanent facts
    (`expires_at IS NULL`) are never touched.
  - Context: drop leftover `job_*` junk keys (ContextStore.gc_stale_job_keys —
    a since-removed write path nothing reads).

Everything is archived to JSON + SHA256 BEFORE removal, so a mistake is
recoverable. The caller (daemon) supplies the archive dir. No agent pause is
needed for the current op set: `purge_expired` re-checks each row's expiry at
DELETE time, so if an agent revives an expired-but-unpurged fact between the
export and the purge (kg_assert refreshes its `expires_at` to the future), the
DELETE simply won't match it — the revived fact is preserved. (A future
live-fact prune WOULD need agents paused; this MVP doesn't touch live facts.)
There is no LLM in this path — the "smart" part (judging live facts,
distillation) is the analyst's proposal, which a human reviews; it is NOT
executed here.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

# Credential predicates the engine NEVER purges, even when expired, are sourced
# at call time from the credential-vocabulary seam (generic kernel defaults +
# whatever a downstream skin registers via
# memory.credentials.register_credential_vocab). Imported at the call sites via
# `cred_predicates()`.
from .credentials import cred_predicates  # noqa: E402

# Refuse to apply if the KG's ACTIVE-fact count is below this — a tripwire
# against a misconfigured/empty store. Expired-only purge doesn't change the
# active count, so this mainly guards future live-prune ops.
DEFAULT_SAFETY_FLOOR = 1


def survey(kg: Any, context_store: Any, *, now: float | None = None) -> dict[str, Any]:
    """Read-only: what the deterministic engine WOULD compact. No mutations.
    Mirrors `apply`'s selection so a dry-run matches the real run."""
    now = now if now is not None else time.time()
    expired = kg.export_expired(now, exclude_predicates=cred_predicates())
    job_keys = 0
    if hasattr(context_store, "list_entries"):
        job_keys = sum(
            1 for e in context_store.list_entries() if str(e.get("key", "")).startswith("job_")
        )
    return {
        "kg_expired_noncred": len(expired),
        "context_job_keys": job_keys,
        "kg_active_facts": kg.stats(now).get("total_facts", 0),
    }


def apply(
    kg: Any,
    context_store: Any,
    *,
    archive_dir: str | Path,
    now: float | None = None,
    floor: int = DEFAULT_SAFETY_FLOOR,
) -> dict[str, Any]:
    """Archive-then-remove the safe/reversible compaction set. Returns a report
    including the archive path + SHA256. Refuses (no mutation) if the KG's
    active-fact count is below `floor`. Concurrency-safe without an agent pause
    (see module docstring): the purge re-checks expiry at DELETE time."""
    now = now if now is not None else time.time()
    # Defense-in-depth: a negative floor would disable the tripwire; clamp it
    # so even a bad direct caller can't bypass the guard. (The daemon also
    # rejects negative floors at the command layer.)
    floor = max(0, int(floor))

    active_before = kg.stats(now).get("total_facts", 0)
    if active_before < floor:
        return {
            "ok": False,
            "error": (
                f"KG active facts ({active_before}) below safety floor "
                f"({floor}); refusing to compact"
            ),
        }

    # Select first so archive and purge cover the same rows (agents paused).
    expired = kg.export_expired(now, exclude_predicates=cred_predicates())

    # Archive BEFORE any deletion — recovery point.
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    payload = {
        "ts": now,
        "kind": "kg_compaction",
        "kg_expired_noncred": expired,
    }
    body = json.dumps(payload, indent=2, sort_keys=True)
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    archive_path = archive_dir / f"compact_{stamp}_{sha[:8]}.json"
    archive_path.write_text(body)

    # Now mutate — only after the archive is safely on disk.
    purged = kg.purge_expired(now, exclude_predicates=cred_predicates())
    job_keys_removed = (
        context_store.gc_stale_job_keys() if hasattr(context_store, "gc_stale_job_keys") else 0
    )

    return {
        "ok": True,
        "archive_path": str(archive_path),
        "archive_sha256": sha,
        "kg_expired_purged": purged,
        "context_job_keys_removed": job_keys_removed,
        "kg_active_facts_before": active_before,
        "kg_active_facts_after": kg.stats(now).get("total_facts", 0),
    }


def _triple(d: Any) -> tuple[str, str, str] | None:
    """Coerce a {subject,predicate,object} mapping to a clean (s,p,o) tuple,
    or None if malformed. Tolerant of an LLM-authored plan."""
    if not isinstance(d, dict):
        return None
    s = str(d.get("subject", "")).strip()
    p = str(d.get("predicate", "")).strip()
    o = str(d.get("object", "")).strip()
    if not (s and p and o):
        return None
    return (s, p, o)


def curate(
    kg: Any,
    *,
    plan: Any,
    archive_dir: str | Path,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Operator-curated merge/dedupe: delete the SPECIFIC duplicate facts the
    operator approved (the analyst's `curate_plan`), keeping a survivor per
    group. Unlike `apply`, this MAY remove live/permanent facts — that's the
    point — but the guardrails make it safe:

      - CREDENTIAL-predicate facts are ALWAYS refused (never deleted).
      - A group whose survivor doesn't resolve is SKIPPED entirely (never
        delete members when the keeper is already gone).
      - A delete that equals the survivor, or that doesn't resolve, is skipped.
      - Identity is EXACT (s,p,o) match (kg.get_exact) — a mis-transcribed
        triple is a no-op, never a wrong delete.
      - All to-delete facts are archived (JSON + SHA256) BEFORE any deletion.

    `plan` is the parsed JSON list of {survivor, deletes[], reason}. With
    `dry_run=True`, resolves + validates and reports what WOULD happen, mutating
    nothing."""
    now = now if now is not None else time.time()
    if not isinstance(plan, list):
        return {"ok": False, "error": "curate_plan must be a JSON list of groups"}

    to_delete: list[dict[str, Any]] = []  # resolved Fact payloads
    refused_credential: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    survivors_kept: list[dict[str, str]] = []
    groups_skipped_no_survivor = 0
    groups_seen = 0

    for group in plan:
        if not isinstance(group, dict):
            continue
        groups_seen += 1
        surv = _triple(group.get("survivor"))
        if surv is None or kg.get_exact(*surv) is None:
            # No keeper → don't touch this group's members.
            groups_skipped_no_survivor += 1
            continue
        survivors_kept.append({"subject": surv[0], "predicate": surv[1], "object": surv[2]})
        for d in group.get("deletes") or []:
            t = _triple(d)
            if t is None:
                continue
            if t == surv:
                continue  # never delete the survivor
            if t[1] in cred_predicates():
                refused_credential.append({"subject": t[0], "predicate": t[1], "object": t[2]})
                continue
            fact = kg.get_exact(*t)
            if fact is None:
                missing.append({"subject": t[0], "predicate": t[1], "object": t[2]})
                continue
            to_delete.append(fact.to_payload())

    report: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "groups": groups_seen,
        "groups_skipped_no_survivor": groups_skipped_no_survivor,
        "would_delete" if dry_run else "deleted": len(to_delete),
        "refused_credential": refused_credential,
        "missing": missing,
        "survivors_kept": survivors_kept,
    }
    if dry_run or not to_delete:
        return report

    # Archive BEFORE deleting — recovery point.
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    body = json.dumps(
        {"ts": now, "kind": "kg_curate", "deleted_facts": to_delete}, indent=2, sort_keys=True
    )
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    archive_path = archive_dir / f"curate_{stamp}_{sha[:8]}.json"
    archive_path.write_text(body)

    for f in to_delete:
        kg.delete(f["id"])

    report["archive_path"] = str(archive_path)
    report["archive_sha256"] = sha
    report["kg_active_facts_after"] = kg.stats(now).get("total_facts", 0)
    return report


def compact_study(
    kg: Any,
    *,
    study_id: str,
    current_doc_shas: Any,
    archive_dir: str | Path,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Namespace-scoped, archive-first compaction for ONE study project.

    The global `apply` only touches EXPIRED facts; study material is permanent,
    so it is never compacted there. This is the study twin: it COMPRESSES a
    project by reclaiming the chunk/doc facts of SUPERSEDED document versions —
    when a document is re-uploaded under a new content hash, the prior version's
    `study:<id>:chunk:*` and `study:<id>:doc:<sha8>` facts become dead weight.
    They are archived to JSON + SHA256 FIRST, then purged (recoverable). Docs
    whose hash is still current are NEVER touched.

    `current_doc_shas`: content hashes of the documents still in the project
    (any length; compared on the first 8 chars, matching the doc-subject naming
    `study:<id>:doc:<sha8>`). `dry_run=True` reports without mutating.
    """
    now = now if now is not None else time.time()
    ns = f"study:{study_id}:"
    current_subjects = {f"{ns}doc:{str(s)[:8]}" for s in (current_doc_shas or [])}
    facts = kg.export_by_subject_prefix(ns, now=now)

    def _is_superseded_doc(subj: str) -> bool:
        return subj.startswith(f"{ns}doc:") and subj not in current_subjects

    # Chunk subjects whose `from_doc` edge points at a superseded doc version.
    superseded_chunks: set[str] = {
        str(f.get("subject", ""))
        for f in facts
        if f.get("predicate") == "from_doc"
        and str(f.get("object", "")).startswith(f"{ns}doc:")
        and str(f.get("object", "")) not in current_subjects
    }

    to_delete = [
        f
        for f in facts
        if str(f.get("subject", "")) in superseded_chunks
        or _is_superseded_doc(str(f.get("subject", "")))
    ]

    report: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "study_id": study_id,
        "superseded_docs": sorted(
            {str(f["subject"]) for f in to_delete if _is_superseded_doc(str(f["subject"]))}
        ),
        ("would_purge" if dry_run else "purged"): len(to_delete),
        "facts_remaining": max(0, len(facts) - len(to_delete)),
    }
    if dry_run or not to_delete:
        return report

    # Archive BEFORE deleting — recovery point (same invariant as apply/curate).
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    body = json.dumps(
        {"ts": now, "kind": "study_compaction", "study_id": study_id, "purged_facts": to_delete},
        indent=2,
        sort_keys=True,
    )
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    archive_path = archive_dir / f"study_{study_id}_compact_{stamp}_{sha[:8]}.json"
    archive_path.write_text(body)

    for f in to_delete:
        kg.delete(f["id"])

    report["archive_path"] = str(archive_path)
    report["archive_sha256"] = sha
    return report
