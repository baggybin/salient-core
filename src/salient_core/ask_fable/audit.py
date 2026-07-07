"""Append-only decision log for ask_fable.

One JSONL record per call — allowed / denied / refused / error — so an operator
can review what was asked of Fable and what the gate did. The question is HASHED
by default (code may be proprietary); set ``ASK_FABLE_AUDIT_RAW=1`` to store the
raw text. Audit I/O never fails the tool: a write error is warned to stderr and
swallowed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _state_dir() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state) / "salient" / "ask_fable"


def audit_path() -> Path:
    override = os.environ.get("ASK_FABLE_AUDIT_PATH")
    if override:
        return Path(override)
    return _state_dir() / "decisions.jsonl"


def _raw_enabled() -> bool:
    return (os.environ.get("ASK_FABLE_AUDIT_RAW") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def record(
    *,
    decision: str,
    stage: str | None,
    reason: str,
    question: str,
    context: str = "",
    session: str = "default",
    model: str = "claude-fable-5",
    duration_ms: int | None = None,
    outcome_detail: str = "",
) -> None:
    """Append one decision record. Best-effort — never raises."""
    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        rec: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "event_id": str(uuid.uuid4()),
            "decision": decision,
            "stage": stage,
            "reason": reason,
            "session": session,
            "model": model,
            "question_len": len(question or ""),
            "context_len": len(context or ""),
            "question_sha256": hashlib.sha256((question or "").encode("utf-8")).hexdigest(),
            "duration_ms": duration_ms,
            "outcome_detail": outcome_detail,
        }
        if _raw_enabled():
            rec["question_raw"] = question
            rec["context_raw"] = context
        new_file = not path.exists()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        if new_file:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001 — audit must never break the tool
        print(f"ask_fable: audit write failed: {exc}", file=sys.stderr)
