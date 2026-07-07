"""Action ledger — per-engagement record of every tool invocation.

Every tool call an agent makes lands here as a row: which agent, which
tool, on what target, with what args, when it started/finished, and a
one-line summary of the outcome. This is the queryable surface that
lets agents (and the daemon's prompt-injection step) answer:

    "Has anyone already run this against this target?"

Different from `kg_facts` in `kg.py`: that store is for CROSS-engagement
FINDINGS (host has_service http/8080). The ledger stores raw ATTEMPTS
within ONE engagement so we can dedup work and surface prior outcomes.

Different from the `events` table in `bus.py`: that table is the verbose
firehose (every text/thinking/tool-call/tool-result) keyed by free-form
JSON. The ledger is the structured, indexed, target-keyed view of just
tool invocations.

Storage lives in the engagement DB, so a new engagement starts with a
clean ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent        TEXT    NOT NULL,
    job_id       INTEGER,
    tool         TEXT    NOT NULL,       -- prettified ("search.query", "fetch_url")
    args_hash    TEXT    NOT NULL,       -- sha1(canonical_json)[:12]
    args_json    TEXT    NOT NULL,       -- canonical JSON (sorted keys)
    target_key   TEXT,                   -- "host:1.2.3.4" / "url:http://..." / NULL
    started_ts   REAL    NOT NULL,
    finished_ts  REAL,
    outcome      TEXT,                   -- "ok" / "error" / NULL while in-flight
    summary      TEXT                    -- one-line head of the result
);
CREATE INDEX IF NOT EXISTS actions_target_ts ON actions(target_key, started_ts DESC);
CREATE INDEX IF NOT EXISTS actions_dedup     ON actions(tool, args_hash, started_ts DESC);
CREATE INDEX IF NOT EXISTS actions_agent_ts  ON actions(agent, started_ts DESC);
"""


def canonical_args(args: Any) -> tuple[str, str]:
    """Return (canonical_json, args_hash) for a tool's input.

    Canonical form uses sort_keys=True so dict insertion order doesn't
    change the hash — same call shape always gets the same args_hash.
    """
    try:
        canon = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canon = json.dumps({"_repr": repr(args)})
    h = hashlib.sha1(canon.encode()).hexdigest()[:12]
    return canon, h


@dataclass
class Action:
    id: int
    agent: str
    job_id: int | None
    tool: str
    args_hash: str
    args_json: str
    target_key: str | None
    started_ts: float
    finished_ts: float | None
    outcome: str | None
    summary: str | None

    def to_line(self) -> str:
        """One-line rendering for prompt injection and operator views."""
        when = time.strftime("%H:%M", time.localtime(self.started_ts))
        target = self.target_key or "-"
        outcome = self.outcome or "…"
        summary = (self.summary or "").splitlines()[0] if self.summary else ""
        if len(summary) > 60:
            summary = summary[:59] + "…"
        return (
            f"{when}  {self.agent[:12]:<12} {self.tool[:20]:<20} "
            f"{target[:30]:<30} {outcome[:7]:<7} {summary}"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "job_id": self.job_id,
            "tool": self.tool,
            "args_hash": self.args_hash,
            "target_key": self.target_key,
            "started_ts": self.started_ts,
            "finished_ts": self.finished_ts,
            "outcome": self.outcome,
            "summary": self.summary,
        }


class ActionLedger:
    """SQLite-backed ledger of tool invocations. One connection per
    instance, RLock-serialized writes. Shares the engagement DB file
    with ContextStore — WAL mode means concurrent writes from both
    connections work fine."""

    def __init__(self, db_path: Path) -> None:
        self._lock = threading.RLock()
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(db_path), check_same_thread=False
        )
        # The ledger keeps verbatim tool args (incl. creds) for prior_actions
        # retry, so it can't be content-redacted — protect it at rest instead.
        # Dir 0700 covers the WAL/-shm sidecars.
        with suppress(OSError):
            os.chmod(db_path.parent, 0o700)
            os.chmod(db_path, 0o600)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def record_start(
        self,
        *,
        agent: str,
        job_id: int | None,
        tool: str,
        args: Any,
        target_key: str | None,
        ts: float | None = None,
    ) -> int:
        """Insert a row for a tool call that just kicked off. Returns
        the action id so the matching `record_finish` can fill in the
        outcome."""
        args_canon, args_hash = canonical_args(args)
        started = ts if ts is not None else time.time()
        with self._lock:
            if self._conn is None:
                return 0  # store closed (shutdown race) — drop the write
            cur = self._conn.execute(
                "INSERT INTO actions "
                "(agent, job_id, tool, args_hash, args_json, target_key, started_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent, job_id, tool, args_hash, args_canon, target_key, started),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def record_finish(
        self,
        action_id: int,
        *,
        outcome: str,
        summary: str | None,
        ts: float | None = None,
    ) -> None:
        """Update the row inserted by `record_start` with the outcome."""
        finished = ts if ts is not None else time.time()
        s = (summary or "").strip()
        if len(s) > 500:
            s = s[:499] + "…"
        with self._lock:
            if self._conn is None:
                return  # store closed (shutdown race) — drop the write
            self._conn.execute(
                "UPDATE actions SET finished_ts=?, outcome=?, summary=? WHERE id=?",
                (finished, outcome, s, action_id),
            )
            self._conn.commit()

    def query(
        self,
        *,
        target: str | None = None,
        tool: str | None = None,
        agent: str | None = None,
        since_ts: float | None = None,
        limit: int = 20,
    ) -> list[Action]:
        """Filter actions by target / tool / agent / age. Substring
        match for target and tool (case-sensitive in SQLite's LIKE
        unless we collate — for IPs and tool names that's fine).
        Newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if target:
            clauses.append("target_key LIKE ?")
            params.append(f"%{target.strip()}%")
        if tool:
            clauses.append("tool LIKE ?")
            params.append(f"%{tool.strip()}%")
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if since_ts is not None:
            clauses.append("started_ts >= ?")
            params.append(float(since_ts))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, agent, job_id, tool, args_hash, args_json, target_key, "
            "started_ts, finished_ts, outcome, summary FROM actions"
            + where
            + " ORDER BY started_ts DESC LIMIT ?"
        )
        params.append(max(1, int(limit)))
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(sql, params).fetchall()
        return [Action(*row) for row in rows]

    def get(self, action_id: int) -> Action | None:
        """Fetch one action row by id (used by on-demand verification to
        reconstruct a claim). None if absent or the store is closed."""
        with self._lock:
            if self._conn is None:
                return None
            row = self._conn.execute(
                "SELECT id, agent, job_id, tool, args_hash, args_json, "
                "target_key, started_ts, finished_ts, outcome, summary "
                "FROM actions WHERE id=?",
                (int(action_id),),
            ).fetchone()
        return Action(*row) if row else None

    def recent_for_targets(
        self,
        target_keys: list[str],
        *,
        per_target_limit: int = 5,
        overall_limit: int = 20,
    ) -> list[Action]:
        """Union of `query(target=tk)` across `target_keys`, deduped by
        id, newest first. Used for the per-job prompt injection — agents
        see what's already been tried against the targets in their task."""
        if not target_keys:
            return []
        seen: dict[int, Action] = {}
        for tk in target_keys:
            for a in self.query(target=tk, limit=per_target_limit):
                seen[a.id] = a
        rows = sorted(seen.values(), key=lambda a: a.started_ts, reverse=True)
        return rows[:overall_limit]

    def count_recent(
        self,
        *,
        tool: str,
        args_hash: str,
        since_ts: float,
    ) -> int:
        """Count rows matching exact tool + args_hash since `since_ts`.
        Used by the daemon's cross-job loop detector — answers "how many
        times has this exact (tool, args) call happened engagement-wide
        in the last N minutes?" Hits the `actions_dedup` index, so this
        is a covered-index lookup even when the table grows large."""
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT COUNT(*) FROM actions WHERE tool = ? AND args_hash = ? AND started_ts >= ?",
                (tool, args_hash, float(since_ts)),
            ).fetchone()
        return int(row[0]) if row else 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            assert self._conn is not None
            total = self._conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
            by_tool = self._conn.execute(
                "SELECT tool, COUNT(*) FROM actions GROUP BY tool ORDER BY 2 DESC LIMIT 10"
            ).fetchall()
            by_agent = self._conn.execute(
                "SELECT agent, COUNT(*) FROM actions GROUP BY agent ORDER BY 2 DESC LIMIT 10"
            ).fetchall()
        return {
            "total": total,
            "by_tool": [{"tool": t, "count": c} for t, c in by_tool],
            "by_agent": [{"agent": a, "count": c} for a, c in by_agent],
        }

    def close(self) -> None:
        # Null ``_conn`` under the lock (idempotent, matches ContextStore): an
        # offloaded ``record_start``/``record_finish`` worker that races
        # shutdown then sees ``_conn is None`` and drops the write cleanly,
        # instead of touching a closed sqlite handle from another thread (UB).
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# ─── Target extraction helpers ─────────────────────────────────────────────
#
# Reuse the scope module's per-tool extractor specs to derive a canonical
# target_key. Falls back to None gracefully when a tool isn't classified
# (bus tools, polling tools, unknown shapes) — the ledger still records
# the action, just without a target_key for queries.


def target_key_for_call(pretty_name: str, args: Any) -> str | None:
    """Pull a canonical target string out of a tool call's args.

    `pretty_name` is the display-friendly tool name (mcp__ stripped,
    __ → .). We try TOOL_TARGETS[full] then TOOL_TARGETS[bare-suffix]
    to find the extractor spec, then use scope.extract_targets and
    take the first target as the key.

    Returns "host:1.2.3.4" / "host:node.example.com" / "url:http://..." /
    None when the tool has no spec, isn't target-bearing, or the
    extractor refused.
    """
    if not isinstance(args, dict):
        return None
    # Lazy import — keeps actions.py importable in tests without pulling
    # the scope module's heavier deps.
    from ..policy.registry import get_active
    from ..policy.scope import ExtractorError, extract_targets

    tool_targets = get_active().tool_targets
    spec = tool_targets.get(pretty_name)
    if spec is None and "." in pretty_name:
        bare = pretty_name.rsplit(".", 1)[-1]
        spec = tool_targets.get(bare)
    if spec is None or spec.none or spec.local_only:
        return None
    try:
        targets = extract_targets(spec, args)
    except ExtractorError:
        return None
    except Exception:  # noqa: BLE001 — never crash a tool call over target extraction
        return None
    if not targets:
        return None
    t = targets[0]
    return f"{t.kind}:{t.value}"


# Regex set for pulling targets out of free-form prompt text. Used when
# we want to surface "prior actions against targets mentioned in this
# task" — we don't have an extractor spec for the prompt itself, so
# fall back to literal IP / hostname / URL recognition.
import re as _re

_IPV4_RE = _re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/\d{1,2})?\b")
_URL_RE = _re.compile(r"\bhttps?://[^\s<>\"'`)]+", _re.IGNORECASE)
_HOST_RE = _re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b",
    _re.IGNORECASE,
)


def extract_target_keys_from_text(text: str) -> list[str]:
    """Find host/IP/URL mentions in free-form text, normalized to the
    same `kind:value` shape `target_key_for_call` writes to the ledger.

    Kinds match `scope.TargetKind`: "ip" / "host" / "network". URLs
    in text are reduced to their hostname (matches what scope does to
    URL args before classifying), so a fetch against
    http://10.0.0.5/foo and a textual mention of 10.0.0.5 produce the
    SAME ledger key — "ip:10.0.0.5" — and `recent_for_targets` finds
    both. Without this alignment, prior-actions injection misses
    matches across the URL/IP boundary.

    Order-preserving, deduplicated, capped to keep prompts compact.
    """
    if not text:
        return []
    import urllib.parse as _urlparse

    seen: dict[str, None] = {}

    def add_host_or_ip(token: str) -> None:
        """Classify a bare token (no scheme) as ip/host and add the key."""
        token = token.strip().rstrip(".").lower()
        if not token:
            return
        # IP shape?
        if _IPV4_RE.fullmatch(token.split("/", 1)[0]) or "/" in token:
            if "/" in token:
                seen.setdefault(f"network:{token}", None)
            else:
                seen.setdefault(f"ip:{token}", None)
            return
        # Otherwise treat as hostname if it has at least one dot.
        if "." in token:
            seen.setdefault(f"host:{token}", None)

    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?)")
        try:
            host = _urlparse.urlparse(url).hostname
        except ValueError:
            host = None
        if host:
            add_host_or_ip(host)

    for m in _IPV4_RE.finditer(text):
        add_host_or_ip(m.group(0))

    for m in _HOST_RE.finditer(text):
        add_host_or_ip(m.group(0))

    return list(seen.keys())[:8]
