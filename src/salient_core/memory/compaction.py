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
recoverable. The caller (daemon) supplies the archive dir. The purge deletes the
archived id set with a delete-time expiry re-check (`kg.purge_expired_ids`),
never a blind re-selection: a fact that expires — or is inserted already-expired
— after the archive is not in the id set and survives to the next run, and a
fact revived (its `expires_at` pushed to the future) before the delete fails the
re-check and is preserved. Archive and deletion cover the same rows, and no
still-live fact is ever purged. (A future live-fact prune WOULD need agents
paused; this MVP doesn't touch live facts.)
There is no LLM in this path — the "smart" part (judging live facts,
distillation) is the analyst's proposal, which a human reviews; it is NOT
executed here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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


def _atomic_write_text(path: Path, body: str) -> None:
    """Write ``body`` to ``path`` durably: write a temp sibling, fsync it, then
    atomically ``os.replace`` over the target. The archive is the ONLY recovery
    point for a compaction, so a torn/partial file from an interrupted write (or
    a reader seeing a half-written archive) would defeat the "archived before
    deleted" invariant. rename-over-target is atomic on POSIX."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # fsync the directory too: the file's contents are durable (fsync above), but
    # the rename/directory entry isn't guaranteed on-disk until the dir is synced.
    # Best-effort — O_DIRECTORY/dir-fsync is POSIX-only; skip cleanly elsewhere.
    try:
        dir_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _delete_facts_atomic(kg: Any, fact_ids: list[int]) -> int:
    """Delete a set of facts all-or-nothing; return the number removed. Prefers
    the KG's transactional ``delete_many`` (one commit for the whole set); falls
    back to per-fact ``delete`` only for a duck-typed KG double that predates
    it."""
    if not fact_ids:
        return 0
    if hasattr(kg, "delete_many"):
        return int(kg.delete_many(fact_ids))
    removed = 0
    for fid in fact_ids:  # pragma: no cover - legacy/test doubles without delete_many
        if kg.delete(fid):
            removed += 1
    return removed


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
    active-fact count is below `floor`. The KG purge deletes the archived id set
    with a delete-time expiry re-check (never a blind re-selection), so archive
    and deletion cover the same rows and no fact that expired-later or was
    revived concurrently is mishandled. The context-store GC is a separate,
    idempotent phase reported independently (see `phases`) — never claimed as
    cross-database-atomic."""
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
    _atomic_write_text(archive_path, body)

    # Now mutate — only after the archive is safely on disk.
    #
    # Phase 1 (KG): delete the archived facts BY ID, re-checking expiry at DELETE
    # time. Two properties, both required:
    #   - by-id (not a re-select): a fact that expires — or is inserted
    #     already-expired — between the export above and this delete is NOT in
    #     the archived id set, so it survives to the next run rather than being
    #     purged without an archive entry.
    #   - re-check expiry/predicate (purge_expired_ids, not a raw delete): a fact
    #     revived (expires_at pushed to the future) or reclassified between the
    #     archive and this delete is left intact — deleting it by id alone would
    #     remove a now-live fact whose current state was never archived.
    expired_ids = [f["id"] for f in expired if isinstance(f, dict) and f.get("id") is not None]
    if hasattr(kg, "purge_expired_ids"):
        purged = kg.purge_expired_ids(expired_ids, now, exclude_predicates=cred_predicates())
    else:  # pragma: no cover - duck-typed KG double without the conditional method
        purged = _delete_facts_atomic(kg, expired_ids)

    # Phase 2 (context store): a SEPARATE database, so we do NOT claim cross-DB
    # atomicity. The KG phase is already archived + committed; this idempotent
    # GC (DELETE ... WHERE key GLOB 'job_*') carries no state a retry would need
    # — it self-heals on the next scheduled run, which re-scans from scratch. So
    # on failure we report a degraded phase and log it (persistent silent
    # failure must surface out-of-band) rather than raising past the completed,
    # load-bearing KG phase.
    job_keys_removed = 0
    context_gc_ok = True
    context_gc_error: str | None = None
    if hasattr(context_store, "gc_stale_job_keys"):
        try:
            job_keys_removed = context_store.gc_stale_job_keys()
        except Exception as exc:  # noqa: BLE001 - best-effort GC; report + log, never crash the run
            context_gc_ok = False
            context_gc_error = repr(exc)
            log.warning(
                "compaction context gc_stale_job_keys failed (self-healing on next run): %s",
                exc,
            )

    return {
        # The KG phase (the load-bearing one) succeeded; the context GC is
        # best-effort, so overall ok stays True and any GC failure is surfaced
        # via `warnings` / `phases` rather than by flipping ok to False.
        "ok": True,
        "archive_path": str(archive_path),
        "archive_sha256": sha,
        "kg_expired_purged": purged,
        "context_job_keys_removed": job_keys_removed,
        "kg_active_facts_before": active_before,
        "kg_active_facts_after": kg.stats(now).get("total_facts", 0),
        "warnings": (
            []
            if context_gc_ok
            else [f"context gc_stale_job_keys failed (self-heals next run): {context_gc_error}"]
        ),
        "phases": {
            "kg_purge": {"ok": True, "count": purged},
            "context_gc": {
                "ok": context_gc_ok,
                "count": job_keys_removed,
                "error": context_gc_error,
                "retry": "automatic-next-run",
            },
        },
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
    _atomic_write_text(archive_path, body)

    # All-or-nothing: one transaction so a mid-set failure can't leave the plan
    # half-applied against an archive that claims every fact was selected.
    _delete_facts_atomic(kg, [f["id"] for f in to_delete])

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
    _atomic_write_text(archive_path, body)

    # All-or-nothing (same invariant as curate).
    _delete_facts_atomic(kg, [f["id"] for f in to_delete])

    report["archive_path"] = str(archive_path)
    report["archive_sha256"] = sha
    return report
