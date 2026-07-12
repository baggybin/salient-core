"""ContextStore — bus-level shared key-value store backed by SQLite.

Extracted from salient/bus.py during the package split. Behavior is
unchanged; only the file location moved.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

_log = logging.getLogger("salient.bus.context_store")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS context (
    agent TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    ts    REAL NOT NULL,
    PRIMARY KEY (agent, key)
);
CREATE INDEX IF NOT EXISTS context_by_agent ON context(agent);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts    REAL NOT NULL
);
-- Verbose per-agent event log (mirror of <engagement>/logs/*.jsonl in
-- queryable form). Every event each agent emits — start, text,
-- thinking, tool-call (with full input), tool-result/error (full text),
-- done, loop, question — is appended here. Drives retrospective
-- queries: tool-call counts per agent per hour, error patterns,
-- delegation graphs, loop hotspots.
--
-- `source` / `recipient` are populated for inter-actor message events
-- (user_message / peer_message / operator_answer) so per-agent
-- transcripts can be reconstructed: "who said what to whom". For
-- ordinary model-side events (text, thinking, tool-call, …) both
-- fields stay NULL — the `agent` column already carries that scope.
CREATE TABLE IF NOT EXISTS events (
    rowid         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    agent         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    job_id        INTEGER,
    tool          TEXT,
    source        TEXT,
    recipient     TEXT,
    engagement_id TEXT,
    content       TEXT NOT NULL    -- JSON payload
);
CREATE INDEX IF NOT EXISTS events_by_agent_ts ON events(agent, ts DESC);
CREATE INDEX IF NOT EXISTS events_by_kind_ts  ON events(kind, ts DESC);
CREATE INDEX IF NOT EXISTS events_by_tool_ts  ON events(tool, ts DESC);
-- events_by_source_ts / events_by_recipient_ts / events_by_engagement_ts
-- are created in their respective additive migrations (so old DBs that
-- don't have the columns yet don't trip them at executescript time).
-- Per-job lifecycle. Survives daemon restart, so AgentRunner.history
-- can be hydrated and the operator's `info <agent>` view doesn't lose
-- everything on a kill/spawn.
CREATE TABLE IF NOT EXISTS jobs (
    rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
    agent        TEXT NOT NULL,
    job_id       INTEGER NOT NULL,    -- runner-local sequence (resets on restart)
    submitted_at REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    prompt       TEXT NOT NULL,
    result       TEXT NOT NULL DEFAULT '',
    error        TEXT,
    prompt_sha   TEXT                 -- sha256 of the agent's prompt file at run time
);
CREATE INDEX IF NOT EXISTS jobs_by_agent_time ON jobs(agent, submitted_at DESC);
-- Prompt-drift provenance: each DISTINCT per-agent prompt-file body an
-- agent has been constructed with, deduped by sha256. Lets `prompt_diff`
-- show what changed since an agent last ran, even across engagements and
-- even if the change was never committed to git. Shared across engagements
-- (one DB), so history accumulates.
CREATE TABLE IF NOT EXISTS prompt_versions (
    agent      TEXT NOT NULL,
    sha        TEXT NOT NULL,
    text       TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    run_count  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (agent, sha)
);
CREATE INDEX IF NOT EXISTS prompt_versions_by_agent
    ON prompt_versions(agent, last_seen DESC);
-- Operator questions filed via ask_operator / <ask_operator> markers.
-- Pending questions hydrate into the inbox at startup so a daemon
-- restart doesn't strand agents that were waiting for an answer.
CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY,
    agent         TEXT NOT NULL,
    text          TEXT NOT NULL,
    job_id        INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL DEFAULT 'operator',
    asked_at      REAL NOT NULL,
    answered_at   REAL,
    answer        TEXT,
    answer_job_id INTEGER,
    answered_by   TEXT
);
CREATE INDEX IF NOT EXISTS questions_pending
    ON questions(answered_at) WHERE answered_at IS NULL;
-- bus_trusted approval-gate bypasses (D-1). When a trusted caller skips the
-- operator agent-start / delegation gate there's no question to record, so the
-- bypass is logged here instead — the durable, queryable system-of-record.
-- Never pruned (unlike the per-agent `events` ring buffer), mirroring how
-- `questions` is treated as the permanent operator-decision log.
CREATE TABLE IF NOT EXISTS approval_bypass (
    rowid         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    caller        TEXT NOT NULL,
    target        TEXT NOT NULL,
    gate          TEXT NOT NULL,          -- 'agent_start' | 'delegation'
    prompt        TEXT,                   -- truncated snippet
    trust_scope   TEXT,                   -- 'all' | 'list'
    engagement_id TEXT
);
CREATE INDEX IF NOT EXISTS approval_bypass_by_ts
    ON approval_bypass(ts DESC);
"""


class ContextStore:
    """Shared key-value store keyed by (agent_name, key).

    If `db_path` is supplied, every write is also persisted to SQLite and the
    in-memory cache is hydrated from disk at startup. If `db_path` is None,
    the store is purely in-memory and cleared when the daemon exits.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        engagement_id: str | None = None,
        events_cap_per_agent: int = 50_000,
        prune_check_interval: int = 1_000,
    ) -> None:
        self._lock = threading.RLock()
        self._mem: dict[tuple[str, str], tuple[str, float]] = {}
        # Daemon-wide singletons keyed by string (active engagement id, etc.).
        # Persisted to the `meta` SQLite table when db_path is set.
        self._meta: dict[str, str] = {}
        self._conn: sqlite3.Connection | None = None
        self._db_path = db_path
        # Engagement stamp: written into every new events row so a future
        # engagement-close DELETE can target rows from one engagement
        # without affecting others. None when the daemon runs without an
        # engagement path — those rows land with NULL.
        self._engagement_id = engagement_id
        # Per-agent ring buffer for events. The bus would otherwise grow
        # unbounded (every text/thinking/tool-call/result is a row). With
        # this cap, an agent at the ceiling sheds its oldest events on
        # the next sweep — bounded growth without a periodic external
        # job. Set <= 0 to disable (full history, e.g. in tests or
        # diagnostic engagements that need replay).
        self._events_cap_per_agent = int(events_cap_per_agent)
        self._prune_check_interval = max(1, int(prune_check_interval))
        # Counter incremented on every record_event; triggers a sweep
        # when it reaches the check interval, then resets to 0.
        self._inserts_since_prune = 0
        # record_event swallows DB write failures (fire-and-forget so it
        # can't crash an agent), but a persistently failing events DB means
        # the operator's audit trail is silently vanishing. Warn once, then
        # stay quiet.
        self._record_warned = False
        # Observable degraded-health. A swallowed audit/recovery write (job,
        # prompt-version, question, answer, approval-bypass, event) keeps agents
        # running but means recovery/audit records are silently vanishing —
        # which changes the system's trust properties. Flip a flag operators can
        # read (`degraded` / `health()`) instead of failing only into a log line.
        self._degraded = False
        self._degraded_reason: str | None = None
        self._degraded_count = 0
        # Per-sink failure counts, so health() can identify WHICH swallowed
        # persistence class is failing (migration vs prune vs event vs …), not
        # just a total. Initialized before migrations run (they report here).
        self._degraded_sinks: dict[str, int] = {}
        # Dedicated lock for the health counters. Every _mark_degraded call runs
        # in an `except` OUTSIDE `self._lock` (the `with self._lock` exits before
        # the exception reaches the handler), and off-loop worker threads write
        # too, so the read-modify-write counters need their own guard for an
        # accurate count + a coherent health() snapshot. Separate from self._lock
        # to keep the critical section tiny. Initialized before migrations run.
        self._health_lock = threading.Lock()
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            # The events table mirrors tool I/O (incl. result text that may
            # carry creds) — keep the DB owner-only at rest. The 0700 dir mode
            # also shields the WAL/-shm sidecars (chmod-per-file would miss
            # those, and they hold recently-committed rows).
            with suppress(OSError):
                os.chmod(db_path.parent, 0o700)
                os.chmod(db_path, 0o600)
            # WAL gives better concurrent-read behavior with one writer.
            self._conn.execute("PRAGMA journal_mode=WAL")
            # WAL allows only one writer DB-wide; without a busy timeout the
            # default is 0ms, so a concurrent writer makes commit() fail
            # instantly (SQLITE_BUSY) — the exact failure that would exercise
            # the rollback path in _txn. A short wait absorbs most of them.
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate_events_source_recipient()
            self._migrate_events_engagement_id()
            self._migrate_jobs_prompt_sha()
            self._migrate_questions_answered_by()
            self._hydrate()

    def _migrate_questions_answered_by(self) -> None:
        """Additive migration: pre-multi-operator DBs have no `answered_by`
        column on `questions`. Old rows get NULL (they predate per-operator
        attribution — correct)."""
        assert self._conn is not None
        try:
            with self._lock, self._txn() as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
                if "answered_by" not in cols:
                    conn.execute("ALTER TABLE questions ADD COLUMN answered_by TEXT")
        except sqlite3.Error as e:
            # An additive schema migration that fails leaves the DB on the old
            # shape; later writes may drop columns. Flip health so it isn't
            # silently invisible (was: bare `pass`).
            self._mark_degraded("migration", e)

    def _migrate_events_source_recipient(self) -> None:
        """Additive migration: pre-2026-05-16 DBs have no `source` or
        `recipient` columns on `events`. ALTER TABLE ADD COLUMN is
        non-destructive in SQLite, so we add them when missing. Old
        rows get NULL for both — correct, since pre-migration events
        weren't tagged with provenance anyway."""
        assert self._conn is not None
        try:
            with self._lock, self._txn() as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
                if "source" not in cols:
                    conn.execute("ALTER TABLE events ADD COLUMN source TEXT")
                if "recipient" not in cols:
                    conn.execute("ALTER TABLE events ADD COLUMN recipient TEXT")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS events_by_source_ts ON events(source, ts DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS events_by_recipient_ts "
                    "ON events(recipient, ts DESC)"
                )
        except sqlite3.Error as e:
            # An additive schema migration that fails leaves the DB on the old
            # shape; later writes may drop columns. Flip health so it isn't
            # silently invisible (was: bare `pass`).
            self._mark_degraded("migration", e)

    def _migrate_events_engagement_id(self) -> None:
        """Additive migration: pre-retention-feature DBs have no
        `engagement_id` column on `events`. Adds the column (NULL for
        old rows — correct, since pre-feature events weren't tagged
        with an engagement) plus its index. Old rows stay NULL; an
        engagement-close DELETE filtering on engagement_id=? will skip
        them, which preserves historical data the operator may still
        want."""
        assert self._conn is not None
        try:
            with self._lock, self._txn() as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
                if "engagement_id" not in cols:
                    conn.execute("ALTER TABLE events ADD COLUMN engagement_id TEXT")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS events_by_engagement_ts "
                    "ON events(engagement_id, ts DESC)"
                )
        except sqlite3.Error as e:
            # An additive schema migration that fails leaves the DB on the old
            # shape; later writes may drop columns. Flip health so it isn't
            # silently invisible (was: bare `pass`).
            self._mark_degraded("migration", e)

    def _migrate_jobs_prompt_sha(self) -> None:
        """Additive migration: pre-prompt-versioning DBs have no
        `prompt_sha` column on `jobs`. ALTER TABLE ADD COLUMN is
        non-destructive; old rows get NULL (correct — they predate the
        feature)."""
        assert self._conn is not None
        try:
            with self._lock, self._txn() as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                if "prompt_sha" not in cols:
                    conn.execute("ALTER TABLE jobs ADD COLUMN prompt_sha TEXT")
        except sqlite3.Error as e:
            # An additive schema migration that fails leaves the DB on the old
            # shape; later writes may drop columns. Flip health so it isn't
            # silently invisible (was: bare `pass`).
            self._mark_degraded("migration", e)

    def _prune_events(self) -> int:
        """Apply the per-agent ring-buffer cap. Returns total rows
        deleted across all agents.

        Two-phase by design:
          1. GROUP BY agent HAVING COUNT(*) > cap — cheap pre-check
             that tells us which agents are over. If empty, the
             expensive DELETE loop is skipped (the common case
             between sweeps).
          2. Per-over-cap agent, DELETE the oldest rows down to the
             cap. The query uses (agent, ts DESC) index for both the
             count and the row selection — each agent's slice is
             touched in isolation rather than scanning the whole table.

        Returns the deleted row count for tests; callers in the hot
        path ignore the return value. DB errors are swallowed (same
        fire-and-forget contract as record_event)."""
        if self._events_cap_per_agent <= 0 or self._conn is None:
            return 0
        cap = self._events_cap_per_agent
        try:
            with self._lock:
                # Re-check under the lock (off-loop writer vs on-loop close()).
                if self._conn is None:
                    return 0
                with self._txn() as conn:
                    over = [
                        row[0]
                        for row in conn.execute(
                            "SELECT agent FROM events GROUP BY agent HAVING COUNT(*) > ?",
                            (cap,),
                        )
                    ]
                    deleted = 0
                    for agent in over:
                        cur = conn.execute(
                            "DELETE FROM events WHERE rowid IN ("
                            " SELECT rowid FROM events WHERE agent=? "
                            " ORDER BY ts ASC "
                            " LIMIT MAX(0, "
                            "   (SELECT COUNT(*) FROM events WHERE agent=?) - ?"
                            " ))",
                            (agent, agent, cap),
                        )
                        deleted += cur.rowcount or 0
                return deleted
        except sqlite3.Error as e:
            # A failed ring-buffer prune lets the events table grow unbounded;
            # surface it in health rather than swallowing silently.
            self._mark_degraded("prune", e)
            return 0

    def _hydrate(self) -> None:
        assert self._conn is not None
        with self._lock:
            self._mem.clear()
            for agent, key, value, ts in self._conn.execute(
                "SELECT agent, key, value, ts FROM context"
            ):
                self._mem[(agent, key)] = (value, ts)
            self._meta.clear()
            for k, v in self._conn.execute("SELECT key, value FROM meta"):
                self._meta[k] = v

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """Run DML in a transaction that rolls back on ANY failure before
        propagating. sqlite3's default isolation opens an implicit transaction
        on the first write; if commit() fails and we do NOT roll back, that
        pending statement stays in the connection and a later unrelated commit()
        flushes it — durable state then diverges from the in-memory cache (which
        we update only after a clean commit). Rolling back on failure discards
        the pending write so no later commit can resurrect it.

        Enter under self._lock with self._conn set. Callers mutate their cache
        only AFTER this context exits cleanly (the failure path re-raises past
        any post-block cache write).

        Note: under sqlite3's legacy isolation only DML (INSERT/UPDATE/DELETE)
        opens the implicit transaction; SELECT/PRAGMA and DDL (ALTER) run in
        autocommit, so this wrapper does not make a multi-statement DDL migration
        atomic. Must not be re-entered while already inside another _txn on this
        connection (a nested commit would prematurely flush the outer work) — the
        non-reentrant self._lock already prevents that by construction."""
        conn = self._conn
        if conn is None:  # defensive: callers guard, and asserts vanish under -O
            raise sqlite3.ProgrammingError("transaction on a closed ContextStore")
        if conn.in_transaction:
            # A transaction is already open on entry => a prior write leaked a
            # dirty transaction (exactly the state this wrapper exists to
            # prevent). Refuse rather than nest onto or flush it.
            raise sqlite3.ProgrammingError(
                "ContextStore connection has a dirty/open transaction on _txn entry"
            )
        try:
            yield conn
            conn.commit()
        except BaseException:
            # BaseException so a cancellation / KeyboardInterrupt mid-commit
            # still clears the dirty transaction before propagating.
            try:
                conn.rollback()
            except sqlite3.Error:
                # Rollback itself failed — the pending statement may still be in
                # the transaction, and reusing this connection would let a later
                # commit() flush it (the original bug). Drop the connection so the
                # store degrades to cache-only rather than silently persisting
                # rejected state; the original failure still propagates below.
                if self._conn is conn:
                    self._conn = None
                with suppress(sqlite3.Error):
                    conn.close()
            raise

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            return self._meta.get(key)

    def meta_keys(self, prefix: str = "") -> list[str]:
        """Daemon-wide meta keys, optionally filtered to those starting with
        `prefix`. Lets callers enumerate a family of singletons (e.g. the
        `study:<id>` project envelopes) without a disk scan. Sorted for
        deterministic listing."""
        with self._lock:
            return sorted(k for k in self._meta if k.startswith(prefix))

    def meta_set(self, key: str, value: str) -> None:
        ts = time.time()
        with self._lock:
            # Commit-first, publish-second: persist to SQLite BEFORE mutating the
            # in-memory cache, so a DB failure raises with the cache untouched
            # rather than leaving an unpersisted value visible until restart
            # (read-your-writes divergence between live and restarted state).
            if self._conn is not None:
                with self._txn() as conn:
                    conn.execute(
                        "INSERT INTO meta (key, value, ts) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                        (key, value, ts),
                    )
            self._meta[key] = value

    def meta_delete(self, key: str) -> bool:
        """Remove a daemon-wide meta singleton. Returns True if it existed.
        Used to fully delete a study project's state envelope."""
        with self._lock:
            existed = key in self._meta
            # Commit-first: durable delete before the cache eviction, so a failed
            # commit leaves the cache consistent with disk.
            if self._conn is not None:
                with self._txn() as conn:
                    conn.execute("DELETE FROM meta WHERE key=?", (key,))
            self._meta.pop(key, None)
            return existed

    def write(self, agent: str, key: str, value: str) -> None:
        ts = time.time()
        with self._lock:
            # Commit-first, publish-second (see meta_set).
            if self._conn is not None:
                with self._txn() as conn:
                    conn.execute(
                        "INSERT INTO context (agent, key, value, ts) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(agent, key) DO UPDATE SET "
                        "value=excluded.value, ts=excluded.ts",
                        (agent, key, value, ts),
                    )
            self._mem[(agent, key)] = (value, ts)

    def read(self, agent: str, key: str) -> str | None:
        with self._lock:
            entry = self._mem.get((agent, key))
            return entry[0] if entry else None

    def delete(self, agent: str, key: str) -> bool:
        with self._lock:
            existed = (agent, key) in self._mem
            # Commit-first: durable delete before cache eviction (see meta_set).
            if self._conn is not None:
                with self._txn() as conn:
                    conn.execute("DELETE FROM context WHERE agent = ? AND key = ?", (agent, key))
            self._mem.pop((agent, key), None)
            return existed

    def keys(self, agent: str | None = None) -> list[tuple[str, str]]:
        with self._lock:
            if agent is None:
                return sorted(self._mem.keys())
            return sorted(k for k in self._mem.keys() if k[0] == agent)

    def list_entries(self, agent: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "agent": a,
                    "key": k,
                    "size": len(self._mem[(a, k)][0]),
                    "ts": self._mem[(a, k)][1],
                    "preview": self._mem[(a, k)][0][:80].replace("\n", " "),
                }
                for (a, k) in (
                    sorted(self._mem.keys())
                    if agent is None
                    else sorted(k for k in self._mem if k[0] == agent)
                )
            ]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> ContextStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Safety net so a caller (or test) that drops the store without an
        # explicit close() doesn't leak the SQLite handle (ResourceWarning).
        # close() is idempotent and lock-guarded; suppress everything since
        # __del__ must never raise during interpreter finalization.
        with suppress(Exception):
            self.close()

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    # ─── Verbose tracking: events / jobs / questions ────────────────────

    def gc_stale_job_keys(self) -> int:
        """Remove leftover `<agent>/job_<N>` rows from a since-removed
        write path (daemon used to dump every job result there but
        nothing reads them — pure SQLite waste). Run at startup. Returns
        deleted row count. Safe: pattern matches `job_<digits>` only."""
        if self._conn is None:
            return 0
        with self._lock:
            with self._txn() as conn:
                cur = conn.execute("DELETE FROM context WHERE key GLOB 'job_*'")
            # Refresh the in-memory cache to drop the deleted entries (only
            # after a clean commit).
            self._mem = {k: v for k, v in self._mem.items() if not k[1].startswith("job_")}
            return cur.rowcount or 0

    def record_event(
        self,
        agent: str,
        kind: str,
        content: Any,
        *,
        tool: str | None = None,
        job_id: int | None = None,
        ts: float | None = None,
        source: str | None = None,
        recipient: str | None = None,
    ) -> None:
        """Append a verbose-tracking event row. Fire-and-forget — DB
        failures are swallowed so logging cannot crash an agent.

        `source` / `recipient` are populated for inter-actor message
        events (user_message / peer_message / operator_answer). For
        ordinary model-side events they stay None — `agent` already
        carries that scope."""
        if self._conn is None:
            return
        try:
            payload = json.dumps(content, default=str)
        except (TypeError, ValueError):
            payload = json.dumps({"_repr": repr(content)})
        try:
            with self._lock:
                # Re-check under the lock: these writes now run on worker
                # threads (off-loop), so the connection can be close()d (also
                # under the lock) between the early no-DB check and here.
                if self._conn is None:
                    return
                with self._txn() as conn:
                    conn.execute(
                        "INSERT INTO events "
                        "(ts, agent, kind, job_id, tool, source, recipient, "
                        " engagement_id, content) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            ts if ts is not None else time.time(),
                            agent,
                            kind,
                            job_id,
                            tool,
                            source,
                            recipient,
                            self._engagement_id,
                            payload,
                        ),
                    )
        except sqlite3.Error as e:
            self._mark_degraded("event", e)
            self._record_warned = True
            return
        # Periodic prune: bounded growth via per-agent ring buffer.
        # Counter check happens OUTSIDE the insert try/except so a
        # commit failure doesn't poison the counter; check is cheap
        # (single int compare + maybe a GROUP BY).
        self._inserts_since_prune += 1
        if self._inserts_since_prune >= self._prune_check_interval:
            self._prune_events()
            self._inserts_since_prune = 0

    def _mark_degraded(self, op: str, exc: BaseException) -> None:
        """Record that a persistence/audit write failed. Sets a sticky health
        flag operators can read; logs once so a full-disk / read-only DB isn't
        wholly invisible, then stays quiet to avoid a log storm."""
        with self._health_lock:
            self._degraded_count += 1
            self._degraded_sinks[op] = self._degraded_sinks.get(op, 0) + 1
            self._degraded_reason = f"{op}: {exc!r}"
            first = not self._degraded
            self._degraded = True
        if first:
            # Log once outside the lock (keeps the critical section tiny), then
            # stay quiet to avoid a log storm on a full-disk / read-only DB.
            _log.warning(
                "context store DEGRADED — %s failed; audit/recovery records may "
                "be lost (silencing further warnings): %r",
                op,
                exc,
            )

    @property
    def degraded(self) -> bool:
        """True once any persistence/audit write has been swallowed."""
        return self._degraded

    def health(self) -> dict[str, Any]:
        """Operator-facing health snapshot. ``ok`` is False after any dropped
        audit/recovery write; ``reason``/``dropped_writes`` give the detail."""
        with self._health_lock:
            return {
                "ok": not self._degraded,
                "degraded": self._degraded,
                "reason": self._degraded_reason,
                # Total swallowed persistence/audit failures (kept as
                # `dropped_writes` for back-compat with existing status callers).
                "dropped_writes": self._degraded_count,
                # {sink -> failure count}: which swallowed persistence class failed
                # and how often (migration / prune / event / job / question / …).
                "sinks": dict(self._degraded_sinks),
            }

    def record_job(
        self,
        agent: str,
        job_id: int,
        prompt: str,
        submitted_at: float,
        started_at: float | None,
        finished_at: float | None,
        result: str,
        error: str | None,
        prompt_sha: str | None = None,
    ) -> None:
        """Persist a completed (or failed) job row. INSERT-only; we
        don't update in place because runner-local job_id resets across
        daemon restarts and we want history to be append-only.

        `prompt_sha` is the sha256 of the agent's prompt-file body at the
        time the runner was constructed — provenance for "what prompt did
        this job run under?"."""
        if self._conn is None:
            return
        try:
            with self._lock:
                # Re-check under the lock (off-loop writer vs on-loop close()).
                if self._conn is None:
                    return
                with self._txn() as conn:
                    conn.execute(
                        "INSERT INTO jobs (agent, job_id, submitted_at, started_at, "
                        "finished_at, prompt, result, error, prompt_sha) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            agent,
                            job_id,
                            submitted_at,
                            started_at,
                            finished_at,
                            prompt,
                            result or "",
                            error,
                            prompt_sha,
                        ),
                    )
        except sqlite3.Error as e:
            self._mark_degraded("job", e)

    def record_prompt_version(self, agent: str, sha: str, text: str) -> None:
        """Snapshot a distinct per-agent prompt body (deduped by sha).
        Called at runner construction. First sighting inserts; repeats
        bump last_seen + run_count. No-op without a DB."""
        if self._conn is None or not sha:
            return
        ts = time.time()
        try:
            with self._lock:
                with self._txn() as conn:
                    conn.execute(
                        "INSERT INTO prompt_versions "
                        "(agent, sha, text, first_seen, last_seen, run_count) "
                        "VALUES (?, ?, ?, ?, ?, 1) "
                        "ON CONFLICT(agent, sha) DO UPDATE SET "
                        "last_seen=excluded.last_seen, run_count=run_count+1",
                        (agent, sha, text, ts, ts),
                    )
        except sqlite3.Error as e:
            self._mark_degraded("prompt_version", e)

    def prompt_versions(self, agent: str) -> list[dict[str, Any]]:
        """Distinct prompt bodies this agent has been constructed with,
        most-recently-seen first. Empty without a DB / no history."""
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT sha, text, first_seen, last_seen, run_count "
                "FROM prompt_versions WHERE agent = ? "
                "ORDER BY last_seen DESC",
                (agent,),
            ).fetchall()
        return [
            {"sha": r[0], "text": r[1], "first_seen": r[2], "last_seen": r[3], "run_count": r[4]}
            for r in rows
        ]

    def latest_prompt_version(self, agent: str) -> dict[str, Any] | None:
        """The most-recently-seen prompt body for an agent, or None."""
        versions = self.prompt_versions(agent)
        return versions[0] if versions else None

    def load_recent_jobs(self, agent: str, limit: int = 100) -> list[dict[str, Any]]:
        """Hydrate the most-recent `limit` jobs for an agent at runner
        construction. Returns plain dicts so the daemon can shape them
        into AgentRunner.history Job records."""
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, prompt, submitted_at, started_at, finished_at, "
                "result, error FROM jobs WHERE agent = ? "
                "ORDER BY submitted_at DESC LIMIT ?",
                (agent, max(1, int(limit))),
            ).fetchall()
        return [
            {
                "job_id": r[0],
                "prompt": r[1],
                "submitted_at": r[2],
                "started_at": r[3],
                "finished_at": r[4],
                "result": r[5] or "",
                "error": r[6],
            }
            for r in rows
        ]

    def record_question(
        self,
        qid: int,
        agent: str,
        text: str,
        job_id: int,
        kind: str,
        asked_at: float,
    ) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                with self._txn() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO questions "
                        "(id, agent, text, job_id, kind, asked_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (qid, agent, text, job_id, kind, asked_at),
                    )
        except sqlite3.Error as e:
            self._mark_degraded("question", e)

    def mark_question_answered(
        self,
        qid: int,
        answer: str,
        answer_job_id: int | None,
        answered_at: float | None = None,
        answered_by: str | None = None,
    ) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                with self._txn() as conn:
                    conn.execute(
                        "UPDATE questions SET answered_at = ?, answer = ?, "
                        "answer_job_id = ?, answered_by = ? WHERE id = ?",
                        (
                            answered_at if answered_at is not None else time.time(),
                            answer,
                            answer_job_id,
                            answered_by,
                            qid,
                        ),
                    )
        except sqlite3.Error as e:
            self._mark_degraded("answer", e)

    def load_pending_questions(self) -> list[dict[str, Any]]:
        """Return unanswered questions for inbox hydration at startup."""
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, agent, text, job_id, kind, asked_at FROM questions "
                "WHERE answered_at IS NULL ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r[0],
                "agent": r[1],
                "text": r[2],
                "job_id": r[3],
                "kind": r[4],
                "asked_at": r[5],
            }
            for r in rows
        ]

    def load_answered_questions(self) -> list[dict[str, Any]]:
        """Return ANSWERED questions — the durable operator-decision record.
        The in-memory QuestionInbox caps how many answered questions it
        retains, so the engagement report sources the COMPLETE decision log
        from here (the store) rather than the bounded live inbox. Returns []
        when no DB is attached (the report then falls back to the inbox)."""
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, agent, text, job_id, kind, asked_at, "
                "answer, answer_job_id, answered_by FROM questions "
                "WHERE answered_at IS NOT NULL ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r[0],
                "agent": r[1],
                "text": r[2],
                "job_id": r[3],
                "kind": r[4],
                "ts": r[5],
                "answered_with": r[6],
                "answer_job_id": r[7],
                "answered_by": r[8],
            }
            for r in rows
        ]

    def record_bypass(
        self,
        caller: str,
        target: str,
        gate: str,
        prompt: str,
        trust_scope: str,
    ) -> None:
        """Persist one bus_trusted approval-gate bypass (D-1). Best-effort,
        like record_event — an audit write must never crash the delegation."""
        if self._conn is None:
            return
        try:
            with self._lock:
                with self._txn() as conn:
                    conn.execute(
                        "INSERT INTO approval_bypass "
                        "(ts, caller, target, gate, prompt, trust_scope, "
                        "engagement_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            time.time(),
                            caller,
                            target,
                            gate,
                            prompt,
                            trust_scope,
                            self._engagement_id,
                        ),
                    )
        except sqlite3.Error as e:
            self._mark_degraded("approval_bypass", e)

    def load_bypasses(
        self,
        *,
        since_ts: float | None = None,
        caller: str | None = None,
        gate: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return recorded approval-gate bypasses (D-1), oldest→newest so the
        operator reads the most recent at the bottom (matches query_events).
        Returns [] when no DB is attached."""
        if self._conn is None:
            return []
        where: list[str] = []
        params: list[Any] = []
        if caller:
            where.append("caller = ?")
            params.append(caller)
        if gate:
            where.append("gate = ?")
            params.append(gate)
        if since_ts is not None:
            where.append("ts >= ?")
            params.append(float(since_ts))
        sql = (
            "SELECT ts, caller, target, gate, prompt, trust_scope "
            "FROM approval_bypass"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY ts DESC LIMIT ?"
        )
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = [
            {
                "ts": r[0],
                "caller": r[1],
                "target": r[2],
                "gate": r[3],
                "prompt": r[4],
                "trust_scope": r[5],
            }
            for r in rows
        ]
        out.reverse()  # oldest first for readable scrollback
        return out

    def query_events(
        self,
        *,
        agent: str | None = None,
        kind: str | None = None,
        tool: str | None = None,
        since_ts: float | None = None,
        job_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query the events table with optional filters. Returns matches
        ordered oldest→newest (tail-style) so the operator reads the
        most recent at the bottom of the screen."""
        if self._conn is None:
            return []
        where: list[str] = []
        params: list[Any] = []
        if agent:
            where.append("agent = ?")
            params.append(agent)
        if job_id is not None:
            where.append("job_id = ?")
            params.append(int(job_id))
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if tool:
            where.append("tool = ?")
            params.append(tool)
        if since_ts is not None:
            where.append("ts >= ?")
            params.append(float(since_ts))
        sql = (
            "SELECT ts, agent, kind, tool, job_id, content FROM events"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY ts DESC LIMIT ?"
        )
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                content = json.loads(r[5]) if r[5] else {}
            except json.JSONDecodeError:
                content = {"_raw": r[5]}
            out.append(
                {
                    "ts": r[0],
                    "agent": r[1],
                    "kind": r[2],
                    "tool": r[3],
                    "job_id": r[4],
                    "content": content,
                }
            )
        out.reverse()  # oldest first for readable scrollback
        return out

    def count_events_by(
        self,
        *,
        by: str = "agent",
        agent: str | None = None,
        kind: str | None = None,
        tool: str | None = None,
        since_ts: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """GROUP BY one of {agent, kind, tool} with COUNT(*).
        Same filter set as query_events. Caller-side validation of `by`
        keeps SQL injection off the table — the column name is
        whitelisted before string-formatting."""
        if self._conn is None:
            return []
        if by not in ("agent", "kind", "tool"):
            raise ValueError(f"by must be agent/kind/tool, got {by!r}")
        where: list[str] = []
        params: list[Any] = []
        if agent:
            where.append("agent = ?")
            params.append(agent)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if tool:
            where.append("tool = ?")
            params.append(tool)
        if since_ts is not None:
            where.append("ts >= ?")
            params.append(float(since_ts))
        sql = (
            f"SELECT COALESCE({by}, '(none)') AS k, COUNT(*) AS n FROM events"
            + (" WHERE " + " AND ".join(where) if where else "")
            + f" GROUP BY {by} ORDER BY n DESC LIMIT ?"
        )
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{"key": r[0], "count": r[1]} for r in rows]

    def max_question_id(self) -> int:
        """Highest assigned question id — used to seed inbox._next_id."""
        if self._conn is None:
            return 0
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM questions").fetchone()
        return int(row[0]) if row else 0
