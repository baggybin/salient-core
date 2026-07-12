"""Knowledge graph — long-lived triple-store for cross-engagement memory.

Salient's context bus (`ContextStore` in `bus.py`) is per-engagement and
keyed by `(agent, key)`. Useful for in-engagement coordination, but
findings vanish when the engagement DB rotates. The KG fills that gap:
**facts that should outlive the engagement they were learned in**.

Storage model: a triple-store. Every fact is a `(subject, predicate,
object)` triple, with provenance metadata (which agent learned it, in
which engagement, when, with what confidence). Triples are flexible —
agents can express anything without pre-defining a schema:

    ("host:node-01",      "has_service",      "service:http/8080")
    ("service:http/8080", "version",          "nginx 1.25.3")
    ("service:http/8080", "depends_on",       "service:db/5432")
    ("host:node-01",      "located_in",       "region:eu-west")
    ("agent:sherlock",    "investigated",     "host:node-01")

Bus tools (added to `bus.py`):
    kg_assert(subject, predicate, object, confidence?)
    kg_query(subject?, predicate?, object?, limit=20)
    kg_neighbors(entity, depth=1, limit=20)

Storage lives at `~/.salient/kg.db` by default (NOT inside the engagement
dir — that's the whole point). Operators can point elsewhere via the
daemon's `--kg-db` flag.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subject       TEXT    NOT NULL,
    predicate     TEXT    NOT NULL,
    object        TEXT    NOT NULL,
    confidence    REAL    NOT NULL DEFAULT 1.0,
    agent         TEXT,
    engagement_id TEXT,
    ts            REAL    NOT NULL,
    expires_at    REAL,
    corroborators TEXT,
    contradiction TEXT
);
CREATE INDEX IF NOT EXISTS kg_subject   ON kg_facts(subject);
CREATE INDEX IF NOT EXISTS kg_predicate ON kg_facts(predicate);
CREATE INDEX IF NOT EXISTS kg_object    ON kg_facts(object);
-- Composite index for "do we already know this?" duplicate-check on insert.
CREATE INDEX IF NOT EXISTS kg_spo       ON kg_facts(subject, predicate, object);
"""
# kg_expires is created AFTER _migrate (in __init__), not in _SCHEMA — on an
# old DB the column doesn't exist until the ALTER lands, so indexing it here
# would fail with "no such column".
_EXPIRES_INDEX = "CREATE INDEX IF NOT EXISTS kg_expires ON kg_facts(expires_at)"

# Default TTL (days) the bus applies to engagement-scoped facts when the
# caller doesn't specify one. NULL expires_at = permanent; this is the
# policy default, configurable via the engagement profile's
# `kg.default_ttl_days`. Lives here so both the store and the bus wrapper
# reference one number. See salient/bus/_kg.py for where it's applied.
_DEFAULT_TTL_DAYS = 30

# Active-fact predicate shared by every read path. A fact is live when it
# has no expiry, or its expiry is still in the future.
_ACTIVE_CLAUSE = "(expires_at IS NULL OR expires_at > ?)"

# Global teaching-meta namespaces that must NEVER surface in the DEFAULT
# (unscoped) semantic search — the cross-engagement pool every agent shares.
# These hold pedagogy/how-to-teach prose (see salient/pedagogy.py); folding it
# into the default pool would let memory-technique passages crowd out real
# engagement/study facts for the tutor AND for any other agent. They
# stay reachable ONLY when a caller passes the matching `subject_prefix`
# explicitly (the tutor does: kg_semantic_query(subject_prefix="pedagogy:")).
_META_PREFIXES: tuple[str, ...] = ("pedagogy:",)


# ── source-provenance resolver seam ──────────────────────────────────────
# A Fact's `source_ref` is an opaque, scheme-tagged pointer ("archive:...",
# "tool:...") back to the raw evidence a fact was distilled from. The kernel
# STORES and surfaces it but NEVER dereferences it — a downstream skin registers
# per-scheme resolvers that load and REDACT the raw evidence (so no unredacted
# tool output can leak through the security-neutral core). Same injection idiom
# as the credential-vocab / kg_assert_hook seams: an un-skinned core has no
# resolvers, so resolution is a no-op and provenance stays display-only.
_SOURCE_RESOLVERS: dict[str, Callable[[str], Any]] = {}


def register_source_resolver(scheme: str, fn: Callable[[str], Any]) -> None:
    """Register a resolver for `source_ref`s of the form ``<scheme>:...`` (e.g.
    ``archive`` or ``tool``). Called once at startup by a downstream skin. The
    resolver receives the FULL source_ref string and returns whatever (already
    redacted) evidence it chooses; the kernel does not interpret the result."""
    _SOURCE_RESOLVERS[scheme.strip().lower()] = fn


def resolve_source_ref(source_ref: str | None) -> Any:
    """Dereference a Fact's `source_ref` via the resolver registered for its
    scheme. Returns None when there's no ref, no scheme, or no matching resolver
    — the kernel never loads raw evidence itself, so an un-skinned core always
    returns None (provenance is display-only until a skin wires a resolver)."""
    if not source_ref:
        return None
    scheme, sep, _rest = source_ref.partition(":")
    if not sep:
        return None
    fn = _SOURCE_RESOLVERS.get(scheme.strip().lower())
    return fn(source_ref) if fn is not None else None


def _like_prefix(prefix: str) -> str:
    """Build a `LIKE ... ESCAPE '\\'` argument that prefix-matches `prefix`
    literally — escaping the LIKE wildcards %/_ (and the escape char) so a
    namespace string like `study:my_proj:` can't act as a pattern. Used by the
    subject-prefix query/export/purge paths."""
    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc + "%"


# ── confidence-weighted corroboration ────────────────────────────────────
# When independent agents assert the SAME triple we pool their per-agent
# confidences via noisy-OR (1 - Π(1 - cᵢ)) — the standard model for combining
# independent evidence (their best confidences live in the `corroborators`
# JSON column {agent: best_conf}; the stored `confidence` is the combined
# value). The combine is over DISTINCT agents — the same agent re-asserting
# only updates its own entry (max), never double-counts. See bus/_kg.py.
_CORROBORATION_CAP = 0.99  # corroboration alone never asserts certainty


def _noisy_or(confidences: Iterable[float]) -> float:
    """Combine independent confidences: 1 - Π(1 - clamp(cᵢ, 0, 1)). Empty → 0.0.
    Monotonic non-decreasing as agents are added (each factor (1-c) ≤ 1), so
    corroboration can never LOWER a fact's confidence."""
    prod = 1.0
    saw = False
    for c in confidences:
        saw = True
        c = min(1.0, max(0.0, float(c)))
        prod *= 1.0 - c
    return 0.0 if not saw else 1.0 - prod


def _combined_confidence(corroborators: dict[str, float]) -> float:
    """Stored confidence derived from the per-agent map. If any single agent
    asserted exactly 1.0 the fact is certain (stays 1.0); otherwise noisy-OR
    over distinct agents, capped at _CORROBORATION_CAP so corroboration alone
    never reaches 1.0. A single agent stores its EXACT confidence (special-
    cased so `1-(1-c)` float drift can't perturb an asserted-once fact)."""
    if not corroborators:
        return 0.0
    vals = [min(1.0, max(0.0, float(c))) for c in corroborators.values()]
    if any(c >= 1.0 for c in vals):
        return 1.0
    if len(vals) == 1:
        return vals[0]
    return min(_CORROBORATION_CAP, _noisy_or(vals))


def _load_corroborators(
    raw: str | None,
    agent: str | None,
    confidence: float,
) -> dict[str, float]:
    """Decode the JSON {agent: conf} map. On NULL/empty/corrupt — legacy rows
    predating the column, or a hand-edited blob — synthesize a single-agent
    map {agent: confidence} so the fact behaves as exactly one corroborator.
    Returns {} when there's no agent to attribute. Never raises into a read."""
    if raw:
        try:
            m = json.loads(raw)
            if isinstance(m, dict) and m:
                return {str(k): float(v) for k, v in m.items()}
        except (ValueError, TypeError):
            pass
    return {agent: float(confidence)} if agent else {}


def _fact_with_corroboration(row: tuple) -> Fact:
    """Build a Fact from a 12-column row — the 9 base columns followed by
    `corroborators` (JSON), `contradiction`, and `source_ref`. Decodes the map
    (with the legacy single-agent fallback) and attaches all three. Used by the
    read paths so agent-facing output can show the corroboration / contradiction
    flags and the provenance pointer."""
    f = Fact(*row[:9])
    f.corroborators = _load_corroborators(row[9], f.agent, f.confidence)
    f.contradiction = row[10]
    f.source_ref = row[11]
    return f


def _fact_with_source(row: tuple) -> Fact:
    """Build a Fact from a 10-column row — the 9 base columns followed by
    `source_ref`. Used by the archive/export paths, which carry provenance (so a
    restored fact keeps its evidence pointer) but not the corroboration map.
    `source_ref` is attached explicitly, never positionally, so it can't land in
    the trailing `corroborators`/`contradiction` slots of `Fact(*row[:9])`."""
    f = Fact(*row[:9])
    f.source_ref = row[9]
    return f


@dataclass
class Fact:
    id: int
    subject: str
    predicate: str
    object: str
    confidence: float
    agent: str | None
    engagement_id: str | None
    ts: float
    # Absolute epoch after which the fact is dead; None = permanent. The base
    # columns end here, so positional `Fact(*row[:9])` unpacking stays valid;
    # the corroboration fields below are trailing + defaulted and attached
    # explicitly by the read paths (they decode JSON the cursor can't).
    expires_at: float | None = None
    # Per-agent best confidences {agent: conf} backing the noisy-OR confidence
    # + the corroboration count. None on legacy rows / facts read without the
    # map; the properties below then treat the fact as single-agent.
    corroborators: dict[str, float] | None = None
    # Free-text note an agent attached flagging a conflict; None = none.
    contradiction: str | None = None
    # Opaque, scheme-tagged pointer back to the raw evidence this fact was
    # distilled from (e.g. "archive:sha256:<hash>#<span>", "tool:nmap#L47").
    # None = no recorded provenance (all legacy facts, and any asserted without
    # one). Trailing + defaulted so `Fact(*row[:9])` positional unpacking stays
    # valid; attached explicitly by the read paths, like corroborators above.
    source_ref: str | None = None

    @property
    def corroboration_count(self) -> int:
        """Distinct agents that asserted this triple. A fact with a populated
        map counts its keys; a legacy/single-agent fact (no map) counts as 1
        when it has an agent, else 0."""
        if self.corroborators:
            return len(self.corroborators)
        return 1 if self.agent else 0

    @property
    def corroborated(self) -> bool:
        """True once ≥2 distinct agents have independently asserted the triple."""
        return self.corroboration_count >= 2

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "confidence": self.confidence,
            "agent": self.agent,
            "engagement_id": self.engagement_id,
            "ts": self.ts,
            "expires_at": self.expires_at,
            "corroborators": self.corroborators or None,
            "corroboration_count": self.corroboration_count,
            "corroborated": self.corroborated,
            "contradiction": self.contradiction,
            "source_ref": self.source_ref,
        }

    def __str__(self) -> str:
        prov = f" [{self.agent}/{self.engagement_id or '?'}]" if self.agent else ""
        corr = f" [corroborated ×{self.corroboration_count}]" if self.corroborated else ""
        flag = " [CONTRADICTION FLAGGED]" if self.contradiction else ""
        src = " [src]" if self.source_ref else ""
        return f"({self.subject}) -[{self.predicate}]-> ({self.object}){prov}{corr}{flag}{src}"


class KnowledgeGraph:
    """SQLite-backed triple store. One writer, snapshot-isolated readers:
    mutating methods serialize on a single locked write connection and run
    inside a real transaction (commit on success, ROLLBACK on exception);
    read methods use per-thread READ-ONLY connections with no lock — WAL
    gives each a consistent snapshot concurrent with writes, so a slow
    reader never blocks the writer or other readers."""

    def __init__(self, db_path: Path) -> None:
        self._lock = threading.RLock()
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(db_path), check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Wait out a concurrent writer instead of failing fast; NORMAL is
        # durable enough under WAL (a power cut loses at most the last
        # transaction, never corrupts) and avoids FULL's fsync-per-commit.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        # Safe now: _SCHEMA created the column on a fresh DB and _migrate
        # ALTERed it onto an old one, so expires_at is guaranteed present.
        self._conn.execute(_EXPIRES_INDEX)
        self._conn.commit()
        # Reader-side state: one READ-ONLY connection per thread, created
        # lazily by _read_conn and tracked so close() can close them all.
        self._local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []

    def _migrate(self) -> None:
        """Additive, idempotent schema migrations for DBs created before a
        column existed. `CREATE TABLE IF NOT EXISTS` won't alter an existing
        table, so a column added to `_SCHEMA` needs an explicit ALTER guarded
        by a presence check. Cheap; runs on every open."""
        assert self._conn is not None
        cols = {
            row[1]  # PRAGMA table_info → (cid, name, type, notnull, dflt, pk)
            for row in self._conn.execute("PRAGMA table_info(kg_facts)")
        }
        if "expires_at" not in cols:
            # Existing rows get NULL = permanent — they predate expiry policy.
            self._conn.execute("ALTER TABLE kg_facts ADD COLUMN expires_at REAL")
        # Semantic-recall columns (added 2026-06). Nullable; a fact with no
        # embedding simply never surfaces in semantic_query and behaves exactly
        # as today in the LIKE/recency paths. `embed_model` records WHICH model
        # produced the vector so the index skips mismatches and backfill can
        # re-embed after a model change. Not in _SCHEMA on purpose — _migrate
        # adds them on both fresh and existing DBs (runs on every open).
        if "embedding" not in cols:
            self._conn.execute("ALTER TABLE kg_facts ADD COLUMN embedding BLOB")
        if "embed_model" not in cols:
            self._conn.execute("ALTER TABLE kg_facts ADD COLUMN embed_model TEXT")
        # Confidence-weighted corroboration (added 2026-06). `corroborators`
        # holds a JSON {agent: best_conf} map backing the noisy-OR confidence
        # and the corroboration count; `contradiction` holds a free-text note
        # an agent attached flagging a conflict. Both nullable — a legacy row
        # with NULL corroborators reads as a single-agent fact (see
        # _load_corroborators) and behaves exactly as before.
        if "corroborators" not in cols:
            self._conn.execute("ALTER TABLE kg_facts ADD COLUMN corroborators TEXT")
        if "contradiction" not in cols:
            self._conn.execute("ALTER TABLE kg_facts ADD COLUMN contradiction TEXT")
        # Tutor spaced-repetition schedule (added 2026-06). `review_due` is the
        # next-review epoch for a learner mastery fact (subject `learner:op`).
        # It is INDEPENDENT of expires_at: learner facts are a gradebook, kept
        # permanent (expires_at NULL) so an OVERDUE review never purges itself
        # out of existence — review_due <= now simply means "due". Nullable; a
        # non-learner fact leaves it NULL and is wholly unaffected.
        if "review_due" not in cols:
            self._conn.execute("ALTER TABLE kg_facts ADD COLUMN review_due REAL")
        # Provenance pointer (added 2026-07). `source_ref` is one OPAQUE,
        # scheme-tagged string linking a fact back to the raw evidence it was
        # distilled from — e.g. "archive:sha256:<hash>#<span>" or "tool:nmap#L47".
        # The kernel only STORES and surfaces it; a downstream skin registers
        # resolvers (register_source_resolver) that dereference + redact it.
        # Nullable; a legacy row leaves it NULL and behaves exactly as before.
        # Set only by trusted internal callers (the demotion distiller, the
        # curator) — NOT the agent-facing kg_assert — so provenance can't be
        # forged. _migrate-only (like embedding/review_due), runs on every open.
        if "source_ref" not in cols:
            self._conn.execute("ALTER TABLE kg_facts ADD COLUMN source_ref TEXT")

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ── connection discipline ───────────────────────────────────────────
    # One writer, snapshot-isolated readers. Writers serialize on the lock
    # and run inside a real transaction; readers take NO lock — each thread
    # gets its own read-only connection and WAL snapshot isolation, so a
    # slow read (semantic_query's fetchall over embedding blobs) blocks
    # neither the writer nor other readers.

    @contextmanager
    def _write_txn(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers and make each write transactional: the
        connection context manager commits on success and ROLLS BACK on
        exception, so a failed write can never leak an open transaction
        (and half a write) to the next lock-holder."""
        with self._lock:
            assert self._conn is not None
            with self._conn:
                yield self._conn

    def _read_conn(self) -> sqlite3.Connection | None:
        """This thread's READ-ONLY connection, or None once the store is
        closed. mode=ro means a coding mistake in a read path can never
        write; the 5s timeout waits out WAL checkpoint edges."""
        if self._conn is None:
            return None
        conn = getattr(self._local, "read_conn", None)
        if conn is None:
            conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=5.0,
            )
            with self._lock:
                if self._conn is None:  # closed while we were connecting
                    conn.close()
                    return None
                self._read_conns.append(conn)
            self._local.read_conn = conn
        return conn

    # ── writes ──────────────────────────────────────────────────────────

    def assert_fact(
        self,
        subject: str,
        predicate: str,
        object_: str,
        *,
        confidence: float = 1.0,
        agent: str | None = None,
        engagement_id: str | None = None,
        expires_at: float | None = None,
        contradicts: str | None = None,
        source_ref: str | None = None,
        dedupe: bool = True,
    ) -> Fact:
        """Record (subject, predicate, object). When `dedupe` is True (default)
        and an identical triple already exists, MERGE this assertion into the
        existing row rather than inserting a duplicate, and return the Fact.

        Corroboration: the asserting `agent` is folded into the row's per-agent
        confidence map ({agent: best_conf}); the stored `confidence` is the
        noisy-OR over DISTINCT agents (so two independent agents at 0.6 + 0.7
        give 0.88, not max=0.7). The same agent re-asserting only raises its own
        entry — never double-counts. A fact asserted once keeps confidence ==
        its single value. See `_combined_confidence`.

        `contradicts` (optional): a note/value this assertion conflicts with.
        Stored on the row and returned on the Fact so the bus wrapper can flag
        the operator. A new value overrides; absence preserves any prior flag.

        `source_ref` (optional): an opaque, scheme-tagged pointer back to the raw
        evidence this fact was distilled from (e.g. "archive:sha256:<hash>#<span>").
        Stored verbatim; the kernel never dereferences it (a skin resolver does).
        Same merge semantics as `contradicts` — a new stamp overrides; absence
        preserves the earliest evidence anchor on a re-asserted triple. Set only
        by trusted internal callers, never the agent-facing kg_assert.

        `expires_at` is an absolute epoch after which the fact stops showing
        up in reads (None = permanent). The dedup lookup is intentionally
        NOT filtered by expiry: re-asserting an expired-but-unpurged triple
        REVIVES the existing row (refreshing ts + expires_at) rather than
        inserting a second copy — so recurring knowledge stays one row."""
        s, p, o = subject.strip(), predicate.strip(), object_.strip()
        if not s or not p or not o:
            raise ValueError("subject, predicate, object all required")
        ts = time.time()
        contradiction = (contradicts or "").strip() or None
        src_ref = (source_ref or "").strip() or None
        with self._write_txn() as conn:
            if dedupe:
                row = conn.execute(
                    "SELECT id, confidence, corroborators, contradiction, agent, "
                    "source_ref "
                    "FROM kg_facts "
                    "WHERE subject=? AND predicate=? AND object=? "
                    "LIMIT 1",
                    (s, p, o),
                ).fetchone()
                if row is not None:
                    fid, old_conf, raw_map, old_contra, old_agent, old_ref = row
                    # Seed the map from the EXISTING row's agent on a legacy
                    # NULL map — not the new asserter — so the prior agent's
                    # confidence isn't misattributed.
                    cmap = _load_corroborators(raw_map, old_agent, old_conf)
                    if agent:
                        cmap[agent] = max(cmap.get(agent, 0.0), float(confidence))
                    new_conf = (
                        _combined_confidence(cmap)
                        if cmap
                        else max(float(old_conf), float(confidence))
                    )
                    # New flag overrides; absence keeps any prior contradiction.
                    new_contra = contradiction or old_contra
                    # Same for provenance: a new stamp overrides, absence
                    # preserves the earliest evidence anchor.
                    new_ref = src_ref or old_ref
                    conn.execute(
                        "UPDATE kg_facts SET ts=?, confidence=?, agent=?, "
                        "engagement_id=?, expires_at=?, corroborators=?, "
                        "contradiction=?, source_ref=? WHERE id=?",
                        (
                            ts,
                            new_conf,
                            agent,
                            engagement_id,
                            expires_at,
                            json.dumps(cmap) if cmap else None,
                            new_contra,
                            new_ref,
                            fid,
                        ),
                    )
                    f = Fact(fid, s, p, o, new_conf, agent, engagement_id, ts, expires_at)
                    f.corroborators = cmap or None
                    f.contradiction = new_contra
                    f.source_ref = new_ref
                    return f
            cmap = {agent: float(confidence)} if agent else {}
            stored_conf = _combined_confidence(cmap) if cmap else float(confidence)
            cur = conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, "
                "confidence, agent, engagement_id, ts, expires_at, "
                "corroborators, contradiction, source_ref) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    s,
                    p,
                    o,
                    stored_conf,
                    agent,
                    engagement_id,
                    ts,
                    expires_at,
                    json.dumps(cmap) if cmap else None,
                    contradiction,
                    src_ref,
                ),
            )
            fid = cur.lastrowid or 0
            f = Fact(fid, s, p, o, stored_conf, agent, engagement_id, ts, expires_at)
            f.corroborators = cmap or None
            f.contradiction = contradiction
            f.source_ref = src_ref
            return f

    # ── tutor learner-model scheduling ───────────────────────────────────
    # The tutor stores per-topic mastery as learner-scoped facts (subject
    # `learner:op`, predicate strong_topic/weak_topic, confidence = mastery).
    # These methods give it a DETERMINISTIC write path the max-merge kg_assert
    # can't: confidence is OVERWRITTEN (a lapse may lower it) and a real
    # next-review date (`review_due`) drives spaced repetition. Arithmetic
    # lives in salient/tutor_schedule.py; the agent-facing wrapper is
    # record_review in salient/bus/_kg.py.

    _STRONG_WEAK = ("strong_topic", "weak_topic")

    @staticmethod
    def _prev_interval_days(review_due: float | None, ts: float) -> float | None:
        """Length in days of the review interval just completed (review_due -
        ts), or None when the fact has no review date. The single
        reconstruction behind learner_review_state and learner_profile —
        tutor code feeds it to schedule.retrievability."""
        return (review_due - ts) / 86400 if review_due else None

    def learner_review_state(
        self,
        subject: str,
        object_: str,
    ) -> dict[str, Any] | None:
        """Current scheduling state for the learner topic (subject, object_),
        looked up across both strong/weak predicates and IGNORING expiry (so an
        overdue fact is found + revived, never duplicated). Returns
        {predicate, mastery, prev_interval_days, review_due} or None if the
        topic was never recorded. prev_interval_days is the length of the
        interval just completed (review_due - ts), or None for a first review."""
        s, o = subject.strip(), object_.strip()
        ph = ",".join("?" for _ in self._STRONG_WEAK)
        conn = self._read_conn()
        assert conn is not None
        row = conn.execute(
            f"SELECT predicate, confidence, ts, review_due FROM kg_facts "
            f"WHERE subject=? AND object=? AND predicate IN ({ph}) "
            f"ORDER BY ts DESC LIMIT 1",
            (s, o, *self._STRONG_WEAK),
        ).fetchone()
        if row is None:
            return None
        predicate, conf, ts_, review_due = row
        prev_interval_days = self._prev_interval_days(review_due, ts_)
        return {
            "predicate": predicate,
            "mastery": float(conf),
            "prev_interval_days": prev_interval_days,
            "review_due": review_due,
        }

    def record_learner_review(
        self,
        subject: str,
        object_: str,
        *,
        predicate: str,
        mastery: float,
        review_due: float,
        agent: str | None = None,
        now: float | None = None,
    ) -> Fact:
        """Upsert the learner mastery fact (subject, predicate, object_) with an
        OVERWRITTEN confidence (= mastery) and next-review date, and DELETE any
        twin under the other strong/weak predicate so a topic crossing the
        strong↔weak line never leaves a stale duplicate. The fact is kept
        permanent (expires_at NULL) — it's a gradebook entry; `review_due`, not
        expiry, governs when it resurfaces."""
        s, o = subject.strip(), object_.strip()
        if not s or not o or not predicate.strip():
            raise ValueError("subject, object, predicate all required")
        ts = now if now is not None else time.time()
        cmap = {agent: float(mastery)} if agent else {}
        with self._write_txn() as conn:
            others = [p for p in self._STRONG_WEAK if p != predicate]
            if others:
                ph = ",".join("?" for _ in others)
                conn.execute(
                    f"DELETE FROM kg_facts WHERE subject=? AND object=? AND predicate IN ({ph})",
                    (s, o, *others),
                )
            existing = conn.execute(
                "SELECT id FROM kg_facts WHERE subject=? AND predicate=? AND object=? LIMIT 1",
                (s, predicate, o),
            ).fetchone()
            if existing is not None:
                fid = existing[0]
                conn.execute(
                    "UPDATE kg_facts SET confidence=?, agent=?, ts=?, "
                    "expires_at=NULL, review_due=?, corroborators=? WHERE id=?",
                    (
                        float(mastery),
                        agent,
                        ts,
                        review_due,
                        json.dumps(cmap) if cmap else None,
                        fid,
                    ),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO kg_facts (subject, predicate, object, "
                    "confidence, agent, engagement_id, ts, expires_at, "
                    "review_due, corroborators) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        s,
                        predicate,
                        o,
                        float(mastery),
                        agent,
                        None,
                        ts,
                        None,
                        review_due,
                        json.dumps(cmap) if cmap else None,
                    ),
                )
                fid = cur.lastrowid or 0
        f = Fact(fid, s, predicate, o, float(mastery), agent, None, ts, None)
        f.corroborators = cmap or None
        return f

    def learner_profile(
        self,
        subject: str,
        *,
        now: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Every ACTIVE fact for a learner subject (any predicate), newest
        first, each annotated with its `review_due`, a `due` flag (review_due
        set and <= now), and `prev_interval_days` (the forgetting-curve input,
        same reconstruction as learner_review_state). Drives the web modal's
        skill-map + due-today panel. Strong/weak mastery facts are permanent
        so always present; misconception/profile facts surface while
        unexpired."""
        now = now if now is not None else time.time()
        conn = self._read_conn()
        assert conn is not None
        rows = conn.execute(
            "SELECT id, subject, predicate, object, confidence, agent, "
            f"engagement_id, ts, expires_at, review_due FROM kg_facts "
            f"WHERE subject=? AND {_ACTIVE_CLAUSE} "
            "ORDER BY ts DESC LIMIT ?",
            (subject.strip(), now, int(limit)),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            review_due = r[9]
            out.append(
                {
                    "id": r[0],
                    "subject": r[1],
                    "predicate": r[2],
                    "object": r[3],
                    "confidence": r[4],
                    "agent": r[5],
                    "ts": r[7],
                    "review_due": review_due,
                    "due": review_due is not None and review_due <= now,
                    "prev_interval_days": self._prev_interval_days(review_due, r[7]),
                }
            )
        return out

    # ── embeddings (semantic recall) ──────────────────────────────────
    # kg.py stays embedder-free: it stores/loads BLOBs and ranks pre-embedded
    # vectors. The async embed() (HTTP) happens in the caller (daemon backfill
    # task / bus tool / runner), which then calls these sync helpers.

    def facts_needing_embedding(
        self,
        model: str,
        *,
        limit: int = 200,
        now: float | None = None,
    ) -> list[tuple[int, str]]:
        """Active facts lacking an embedding for `model` (NULL, or embedded under
        a different model). Returns [(id, "subject predicate object")] for the
        caller to embed in a batch."""
        now = now if now is not None else time.time()
        conn = self._read_conn()
        assert conn is not None
        rows = conn.execute(
            "SELECT id, subject, predicate, object FROM kg_facts "
            f"WHERE {_ACTIVE_CLAUSE} "
            "AND (embedding IS NULL OR embed_model IS NOT ?) "
            "ORDER BY ts DESC LIMIT ?",
            (now, model, int(limit)),
        ).fetchall()
        return [(r[0], f"{r[1]} {r[2]} {r[3]}") for r in rows]

    def store_embeddings(
        self,
        items: list[tuple[int, bytes]],
        model: str,
    ) -> int:
        """Batch-store [(fact_id, blob)] under `model`. Returns count."""
        if not items:
            return 0
        with self._write_txn() as conn:
            conn.executemany(
                "UPDATE kg_facts SET embedding=?, embed_model=? WHERE id=?",
                [(blob, model, int(fid)) for fid, blob in items],
            )
        return len(items)

    def embedding_counts(
        self,
        model: str,
        *,
        now: float | None = None,
        subject_prefix: str | None = None,
    ) -> tuple[int, int, int]:
        """``(total, embedded, pending)`` over ACTIVE facts for ``model``, so the
        operator can see how much of the KG is actually searchable by meaning.
        ``pending`` mirrors :meth:`facts_needing_embedding`'s predicate verbatim
        (NULL embedding, or embedded under a different model); ``embedded`` is the
        complement, so ``embedded + pending == total``.

        ``subject_prefix`` (optional) scopes all three counts to ACTIVE facts
        whose subject starts with it (LIKE, escaped) — the namespaced twin of the
        read methods' prefix filter, so a fenced caller's stats can show the
        embedding coverage of ONLY its own namespace. Empty/None = the whole
        graph."""
        now = now if now is not None else time.time()
        prefix_clause = " AND subject LIKE ? ESCAPE '\\'" if subject_prefix else ""
        prefix_params: list[Any] = [_like_prefix(subject_prefix)] if subject_prefix else []
        conn = self._read_conn()
        assert conn is not None
        total = conn.execute(
            f"SELECT COUNT(*) FROM kg_facts WHERE {_ACTIVE_CLAUSE}{prefix_clause}",
            (now, *prefix_params),
        ).fetchone()[0]
        pending = conn.execute(
            f"SELECT COUNT(*) FROM kg_facts WHERE {_ACTIVE_CLAUSE} "
            "AND (embedding IS NULL OR embed_model IS NOT ?)"
            f"{prefix_clause}",
            (now, model, *prefix_params),
        ).fetchone()[0]
        return (int(total), int(total) - int(pending), int(pending))

    def semantic_query(
        self,
        query_vec: list[float],
        *,
        model: str,
        top_k: int = 10,
        min_score: float = 0.5,
        subject_prefix: str | None = None,
        now: float | None = None,
    ) -> list[tuple[Fact, float]]:
        """Cosine-rank ACTIVE facts embedded under `model` against `query_vec`.
        Returns [(Fact, score)] desc, score >= min_score, capped at top_k. Pure
        Python; the caller must embed the query text first (kg holds no embedder).

        `subject_prefix` (optional) scopes the ranked set to facts whose subject
        starts with it — e.g. `study:<id>:` to bind a tutoring session to ONE
        study project so unrelated (or other-engagement) facts never surface.
        Matched in SQL via LIKE; the prefix is escaped so a literal %/_ in it
        can't act as a wildcard. When NO prefix is given (the default read), the
        teaching-meta namespaces in `_META_PREFIXES` (e.g. `pedagogy:`) are
        EXCLUDED, so how-to-teach prose only surfaces when asked for by prefix."""
        from .embeddings import cosine, unpack_vector

        if not query_vec:
            return []
        now = now if now is not None else time.time()
        sql = (
            "SELECT id, subject, predicate, object, confidence, agent, "
            "engagement_id, ts, expires_at, embedding, corroborators, "
            f"contradiction, source_ref FROM kg_facts "
            f"WHERE embed_model=? AND embedding IS NOT NULL AND {_ACTIVE_CLAUSE}"
        )
        params: list[Any] = [model, now]
        if subject_prefix:
            sql += " AND subject LIKE ? ESCAPE '\\'"
            params.append(_like_prefix(subject_prefix))
        else:
            # Unscoped (default) read: exclude the teaching-meta namespaces so
            # pedagogy prose never pollutes the shared cross-engagement pool.
            for mp in _META_PREFIXES:
                sql += " AND subject NOT LIKE ? ESCAPE '\\'"
                params.append(_like_prefix(mp))
        conn = self._read_conn()
        assert conn is not None
        rows = conn.execute(sql, params).fetchall()
        scored: list[tuple[Fact, float]] = []
        for row in rows:
            vec = unpack_vector(row[9])
            if not vec:
                continue
            s = cosine(query_vec, vec)
            if s >= min_score:
                f = Fact(*row[:9])
                f.corroborators = _load_corroborators(row[10], f.agent, f.confidence)
                f.contradiction = row[11]
                f.source_ref = row[12]
                scored.append((f, s))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[: int(top_k)]

    # ── reads ───────────────────────────────────────────────────────────

    def query(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        object_: str | None = None,
        *,
        limit: int = 20,
        subject_prefix: str | None = None,
    ) -> list[Fact]:
        """Pattern-match query. Any of (s, p, o) may be None to wildcard.
        Substring matches (case-insensitive) — use exact strings for exact
        matches when you have them. Ordered by recency.

        ``subject_prefix`` (optional) restricts the result to facts whose
        subject starts with it — matched in SQL (LIKE, escaped) via the
        ``kg_subject`` index so a namespaced caller can scope a read without
        over-fetching. Empty/None = no prefix restriction (the default)."""
        clauses: list[str] = []
        params: list[Any] = []
        if subject_prefix:
            clauses.append("subject LIKE ? ESCAPE '\\'")
            params.append(_like_prefix(subject_prefix))
        for col, val in (("subject", subject), ("predicate", predicate), ("object", object_)):
            if val is not None and str(val).strip():
                clauses.append(f"{col} LIKE ?")
                params.append(f"%{val.strip()}%")
        # Always exclude expired facts (None expires_at = permanent).
        clauses.append(_ACTIVE_CLAUSE)
        params.append(time.time())
        where = " WHERE " + " AND ".join(clauses)
        sql = (
            f"SELECT id, subject, predicate, object, confidence, agent, "
            f"engagement_id, ts, expires_at, corroborators, contradiction, "
            f"source_ref "
            f"FROM kg_facts{where} "
            f"ORDER BY ts DESC LIMIT ?"
        )
        params.append(int(limit))
        conn = self._read_conn()
        assert conn is not None
        rows = conn.execute(sql, params).fetchall()
        return [_fact_with_corroboration(row) for row in rows]

    def get(self, fact_id: int) -> Fact | None:
        """Fetch one fact by id, expired or not (an explicit id lookup — used by
        on-demand verification to reconstruct a claim). None if absent or the
        store is closed."""
        sql = (
            "SELECT id, subject, predicate, object, confidence, agent, "
            "engagement_id, ts, expires_at, corroborators, contradiction, "
            "source_ref "
            "FROM kg_facts WHERE id=?"
        )
        conn = self._read_conn()
        if conn is None:
            return None
        row = conn.execute(sql, (int(fact_id),)).fetchone()
        return _fact_with_corroboration(row) if row else None

    def neighbors(
        self,
        entity: str,
        *,
        depth: int = 1,
        limit: int = 50,
        subject_prefix: str | None = None,
    ) -> list[Fact]:
        """Return facts where ``entity`` appears as either subject or object,
        walking up to ``depth`` hops. Capped at ``limit`` total facts.
        Useful for "tell me everything we know that touches host X."

        ``subject_prefix`` (optional) scopes the walk to facts whose SUBJECT
        starts with it — the namespace read-fence at the kernel tier. The BFS
        only follows and returns in-prefix-subject edges: an out-of-prefix
        entity reached as the OBJECT of an in-prefix fact (e.g.
        ``study:X -concerns-> host:secret``) still seeds further hops, but a
        foreign hub never explodes the frontier — its own facts have
        out-of-prefix subjects and are filtered by the ``kg_subject`` index
        scan, so the walk stays bounded by the namespace size, not the hub's
        degree. Empty/None = walk the whole graph (the default)."""
        seen_facts: dict[int, Fact] = {}
        expanded: set[str] = set()  # entities we've already pulled neighbors for
        frontier: set[str] = {entity.strip()}
        conn = self._read_conn()
        assert conn is not None
        prefix_clause = " AND subject LIKE ? ESCAPE '\\'" if subject_prefix else ""
        prefix_params: tuple[Any, ...] = (_like_prefix(subject_prefix),) if subject_prefix else ()
        now = time.time()
        for _ in range(max(1, depth)):
            if not frontier or len(seen_facts) >= limit:
                break
            next_frontier: set[str] = set()
            for ent in list(frontier):
                if len(seen_facts) >= limit:
                    break
                expanded.add(ent)
                rows = conn.execute(
                    "SELECT id, subject, predicate, object, confidence, "
                    "agent, engagement_id, ts, expires_at, corroborators, "
                    f"contradiction, source_ref FROM kg_facts "
                    f"WHERE (subject=? OR object=?) AND {_ACTIVE_CLAUSE}"
                    f"{prefix_clause} "
                    "ORDER BY ts DESC LIMIT ?",
                    (ent, ent, now, *prefix_params, limit),
                ).fetchall()
                for row in rows:
                    f = _fact_with_corroboration(row)
                    if f.id in seen_facts:
                        continue
                    seen_facts[f.id] = f
                    if len(seen_facts) >= limit:
                        break
                    # next-hop entity (the OTHER endpoint of this fact)
                    other = f.object if f.subject == ent else f.subject
                    if other != ent:
                        next_frontier.add(other)
            # Don't re-expand entities we've already pulled neighbors for.
            # (Don't subtract by "ever appeared" — that would cut off
            # transitive chains where a node is mentioned but never expanded.)
            frontier = next_frontier - expanded
        return list(seen_facts.values())

    def stats(self, now: float | None = None) -> dict[str, Any]:
        """Summary over ACTIVE (non-expired) facts: total, distinct entities,
        engagements, top predicates, plus `expiring_within_7d` — active facts
        whose expiry falls inside the next 7 days. Expired-but-unpurged rows
        are excluded so the numbers reflect usable knowledge (purge runs at
        shutdown, so they only linger mid-run)."""
        now = now if now is not None else time.time()
        active = f"WHERE {_ACTIVE_CLAUSE}"
        conn = self._read_conn()
        assert conn is not None
        total = conn.execute(f"SELECT COUNT(*) FROM kg_facts {active}", (now,)).fetchone()[0]
        entities = conn.execute(
            "SELECT COUNT(DISTINCT s) FROM ("
            f"SELECT subject AS s FROM kg_facts {active} UNION "
            f"SELECT object  AS s FROM kg_facts {active})",
            (now, now),
        ).fetchone()[0]
        preds = conn.execute(
            f"SELECT predicate, COUNT(*) FROM kg_facts {active} "
            "GROUP BY predicate ORDER BY 2 DESC LIMIT 10",
            (now,),
        ).fetchall()
        engs = conn.execute(
            f"SELECT COUNT(DISTINCT engagement_id) FROM kg_facts {active} "
            "AND engagement_id IS NOT NULL",
            (now,),
        ).fetchone()[0]
        expiring = conn.execute(
            "SELECT COUNT(*) FROM kg_facts "
            "WHERE expires_at IS NOT NULL AND expires_at > ? "
            "AND expires_at <= ?",
            (now, now + 7 * 86400),
        ).fetchone()[0]
        return {
            "total_facts": total,
            "distinct_entities": entities,
            "engagements": engs,
            "expiring_within_7d": expiring,
            "top_predicates": [{"predicate": p, "count": c} for p, c in preds],
        }

    def export_expired(
        self,
        now: float | None = None,
        *,
        exclude_predicates: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Return expired facts as payload dicts WITHOUT deleting them — the
        compaction engine archives these before purging. Mirrors the
        `purge_expired` filter (same `exclude_predicates`) so archive and
        purge cover exactly the same rows. Read-only."""
        now = now if now is not None else time.time()
        sql = (
            "SELECT id, subject, predicate, object, confidence, agent, "
            "engagement_id, ts, expires_at, source_ref FROM kg_facts "
            "WHERE expires_at IS NOT NULL AND expires_at <= ?"
        )
        params: list[Any] = [now]
        if exclude_predicates:
            ph = ",".join("?" for _ in exclude_predicates)
            sql += f" AND predicate NOT IN ({ph})"
            params.extend(exclude_predicates)
        conn = self._read_conn()
        assert conn is not None
        rows = conn.execute(sql, params).fetchall()
        return [_fact_with_source(r).to_payload() for r in rows]

    def purge_expired(
        self,
        now: float | None = None,
        *,
        exclude_predicates: tuple[str, ...] = (),
    ) -> int:
        """Delete every fact whose expiry has passed. Returns the number of
        rows removed. Called at daemon shutdown (no exclusions) and by the
        compaction engine, which passes credential predicates to
        `exclude_predicates` so expired credential rows are NEVER removed.
        Safe to call any time."""
        now = now if now is not None else time.time()
        sql = "DELETE FROM kg_facts WHERE expires_at IS NOT NULL AND expires_at <= ?"
        params: list[Any] = [now]
        if exclude_predicates:
            ph = ",".join("?" for _ in exclude_predicates)
            sql += f" AND predicate NOT IN ({ph})"
            params.extend(exclude_predicates)
        with self._write_txn() as conn:
            return conn.execute(sql, params).rowcount

    def purge_expired_ids(
        self,
        fact_ids: Iterable[int],
        now: float,
        *,
        exclude_predicates: tuple[str, ...] = (),
    ) -> int:
        """Delete facts BY ID, but only those STILL expired and non-excluded at
        DELETE time. The compaction engine passes the exact id set it archived,
        so archive and deletion cover the same rows — never a re-selection that
        could delete an unarchived row. The per-row expiry/predicate re-check
        preserves the concurrency guarantee: a fact revived (its ``expires_at``
        pushed to the future) or reclassified to an excluded predicate between
        the archive and this delete is left intact. Returns rows removed; one
        transaction (all-or-nothing).

        ``now`` is REQUIRED and must be the *export* timestamp (the same clock
        used to select + archive the ids), not a fresh delete-time clock — the
        re-check compares against it, so a stale clock could delete a row revived
        to just past ``now``. Known limitation: the re-check covers expiry and
        predicate, not the payload — a row whose content is updated while staying
        expired is still deleted, and the archive then holds the pre-update copy
        (acceptable: the row is expired junk either way, and concurrent updates
        to already-expired rows are rare)."""
        ids = [int(i) for i in fact_ids]
        if not ids:
            return 0
        cond = "id=? AND expires_at IS NOT NULL AND expires_at <= ?"
        tail: list[Any] = []
        if exclude_predicates:
            ph = ",".join("?" for _ in exclude_predicates)
            cond += f" AND predicate NOT IN ({ph})"
            tail = list(exclude_predicates)
        with self._write_txn() as conn:
            deleted = 0
            for fid in ids:
                deleted += conn.execute(
                    f"DELETE FROM kg_facts WHERE {cond}", (fid, now, *tail)
                ).rowcount
            return deleted

    def export_by_subject_prefix(
        self,
        prefix: str,
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return ACTIVE facts whose subject starts with `prefix` as payload
        dicts WITHOUT deleting them — the namespaced twin of `export_expired`,
        used to archive a `study:<id>:` namespace before purge. Mirrors
        `purge_by_subject_prefix`'s filter so archive + purge cover the same
        rows. Read-only. Empty prefix → [] (never dump the whole graph)."""
        if not prefix:
            return []
        now = now if now is not None else time.time()
        like = _like_prefix(prefix)
        conn = self._read_conn()
        assert conn is not None
        rows = conn.execute(
            "SELECT id, subject, predicate, object, confidence, agent, "
            f"engagement_id, ts, expires_at, source_ref FROM kg_facts "
            f"WHERE subject LIKE ? ESCAPE '\\' AND {_ACTIVE_CLAUSE}",
            (like, now),
        ).fetchall()
        return [_fact_with_source(r).to_payload() for r in rows]

    def purge_by_subject_prefix(
        self,
        prefix: str,
        *,
        now: float | None = None,
    ) -> int:
        """Delete every ACTIVE fact whose subject starts with `prefix`; returns
        the row count. The namespaced twin of `purge_expired` — used to drop a
        whole `study:<id>:` namespace (project delete) or, via the compaction
        engine, a superseded document's chunks AFTER they've been archived.
        Empty prefix → 0 (never purge the whole graph)."""
        if not prefix:
            return 0
        now = now if now is not None else time.time()
        like = _like_prefix(prefix)
        with self._write_txn() as conn:
            return conn.execute(
                f"DELETE FROM kg_facts WHERE subject LIKE ? ESCAPE '\\' AND {_ACTIVE_CLAUSE}",
                (like, now),
            ).rowcount

    def delete(self, fact_id: int) -> bool:
        with self._write_txn() as conn:
            return conn.execute("DELETE FROM kg_facts WHERE id=?", (fact_id,)).rowcount > 0

    def delete_many(self, fact_ids: Iterable[int]) -> int:
        """Delete several facts in ONE transaction — all-or-nothing. Returns the
        number of rows removed. Compaction uses this so a failure partway
        through can't leave a curation plan half-applied (some facts deleted,
        some kept) against an archive that claims all were selected: the single
        ``_write_txn`` commits every delete together or rolls the whole set
        back."""
        ids = [int(i) for i in fact_ids]
        if not ids:
            return 0
        with self._write_txn() as conn:
            deleted = 0
            for fid in ids:
                deleted += conn.execute("DELETE FROM kg_facts WHERE id=?", (fid,)).rowcount
            return deleted

    def get_exact(
        self,
        subject: str,
        predicate: str,
        object_: str,
    ) -> Fact | None:
        """Return the single fact matching (subject, predicate, object)
        EXACTLY, or None. `(s,p,o)` is unique by the dedupe contract
        (`kg_spo` index), so this resolves the canonical row the compaction
        curate engine targets. Exact match (not LIKE) — a mis-transcribed
        triple simply returns None, so curate can't delete the wrong fact."""
        conn = self._read_conn()
        assert conn is not None
        row = conn.execute(
            "SELECT id, subject, predicate, object, confidence, agent, "
            "engagement_id, ts, expires_at, corroborators, contradiction, "
            "source_ref "
            "FROM kg_facts "
            "WHERE subject=? AND predicate=? AND object=? LIMIT 1",
            (subject, predicate, object_),
        ).fetchone()
        return _fact_with_corroboration(row) if row is not None else None

    def close(self) -> None:
        # Null ``_conn`` under the lock for idempotency and parity with
        # ContextStore / ActionLedger. KG writes run on the loop thread (not
        # offloaded), so close() can't race a writer here, but an unconditional
        # ``self._conn.close()`` would crash on a double-close.
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            # Per-thread read-only connections were opened with
            # check_same_thread=False, so closing them from here is legal.
            # Threads that race past the _read_conn closed-check keep a
            # working (read-only) connection until process exit; harmless.
            for rc in self._read_conns:
                try:
                    rc.close()
                except sqlite3.Error:
                    pass
            self._read_conns.clear()

    def __enter__(self) -> KnowledgeGraph:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Safety net against a leaked SQLite handle (ResourceWarning) when a
        # caller/test drops the graph without close(). Idempotent + lock-guarded;
        # __del__ must never raise during finalization, so suppress everything.
        try:
            self.close()
        except Exception:
            pass
