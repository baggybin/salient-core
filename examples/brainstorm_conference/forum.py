"""Append-only forum store for the brainstorm-conference example.

This is the one primitive the salient-core kernel does not already provide. The
kernel talks *star-shaped* — directed request/reply (``ask_agent``) — and its
shared blackboard (``context_*``) is last-write-wins per key. Neither gives you
a single thread that many agents post to and all read back *in order*. A
brainstorm conference needs exactly that: a round table where every debater's
contribution is visible to whoever speaks next.

``ForumStore`` is that thread — an append-only log of posts ordered by a
monotonic sequence, filterable by ``since`` so an agent can cheaply read "what's
new since I last looked". Append-only on purpose: a post is never updated or
deleted, so the transcript the rapporteur reads at the end is the transcript
that actually happened.

Pure and dependency-light: stdlib ``sqlite3`` only (in-memory by default, or a
file path for persistence), and **no salient_core import** — so it unit-tests
offline, with no daemon. The ``@bus_tool`` wrappers that expose it to agents
live in ``forum_tools.py``.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ForumEntry:
    """One post on the forum thread. ``seq`` is the global monotonic order —
    stable, gap-free-per-insert, and the cursor a reader passes back as
    ``since`` to page forward."""

    seq: int
    round: int
    agent: str
    text: str
    ts: float

    def render(self) -> str:
        """One-line rendering for a speaker or the rapporteur to read."""
        return f"[#{self.seq} r{self.round} {self.agent}] {self.text}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS forum (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    round  INTEGER NOT NULL,
    agent  TEXT    NOT NULL,
    text   TEXT    NOT NULL,
    ts     REAL    NOT NULL
);
"""


class ForumStore:
    """An append-only, ordered, multi-writer conversation thread.

    Backed by SQLite so it survives a process restart when given a ``db_path``
    (and stays purely in-memory otherwise, for tests and throwaway runs). Every
    write is a single ``INSERT`` — the table only ever grows, and ``seq`` is an
    ``AUTOINCREMENT`` primary key, so ordering is total and monotonic across all
    writers with no application-side counter to get wrong.
    """

    def __init__(self, db_path: str | None = None) -> None:
        # check_same_thread=False: the daemon's async tasks may touch the store
        # from different threads. Each operation here is a single statement, so
        # SQLite's own locking is enough — we add no cross-statement transactions.
        self._db = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(_SCHEMA)
        self._db.commit()

    def post(self, agent: str, text: str, *, round: int = 0) -> ForumEntry:
        """Append one post; return the stored entry with its assigned ``seq``.

        Never updates or deletes — two posts from the same agent in the same
        round are two rows, not one clobbering the other. Raises ``ValueError``
        on empty text (a post with nothing in it is a bug, not a valid turn).
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("forum post text must be non-empty")
        ts = time.time()
        cur = self._db.execute(
            "INSERT INTO forum (round, agent, text, ts) VALUES (?, ?, ?, ?)",
            (int(round), agent, text, ts),
        )
        self._db.commit()
        return ForumEntry(
            seq=int(cur.lastrowid), round=int(round), agent=agent, text=text, ts=ts
        )

    def read(self, *, since: int = 0, limit: int | None = None) -> list[ForumEntry]:
        """Posts with ``seq > since``, oldest first.

        ``since=0`` (the default) returns the whole thread; pass the last ``seq``
        you saw to get only what's new. ``limit`` caps the batch for very long
        threads.
        """
        sql = (
            "SELECT seq, round, agent, text, ts FROM forum "
            "WHERE seq > ? ORDER BY seq ASC"
        )
        params: tuple = (int(since),)
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, int(limit))
        rows = self._db.execute(sql, params).fetchall()
        return [
            ForumEntry(
                seq=r["seq"], round=r["round"], agent=r["agent"], text=r["text"], ts=r["ts"]
            )
            for r in rows
        ]

    def latest_round(self) -> int:
        """Highest round number posted so far (0 when the thread is empty)."""
        row = self._db.execute("SELECT MAX(round) AS r FROM forum").fetchone()
        return int(row["r"]) if row and row["r"] is not None else 0

    def count(self) -> int:
        """Total posts in the thread."""
        row = self._db.execute("SELECT COUNT(*) AS n FROM forum").fetchone()
        return int(row["n"]) if row else 0

    def transcript(self, *, since: int = 0) -> str:
        """The thread rendered as text for a speaker or the rapporteur to read."""
        entries = self.read(since=since)
        return "\n".join(e.render() for e in entries) if entries else "(forum empty)"

    def close(self) -> None:
        self._db.close()
