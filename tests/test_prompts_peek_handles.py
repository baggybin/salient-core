"""Startup SQLite peek helpers must not leak connection handles
(kernel-invariant #7).

``sqlite3``'s own context manager (``with sqlite3.connect(...) as conn``)
commits/rolls back but does NOT close the connection, so the handle stays open
until the garbage collector reclaims it. The fix wraps each reader in
``contextlib.closing`` so the connection is closed deterministically when the
helper returns.

We assert closure directly rather than via ``ResourceWarning``: CPython's
sqlite3 does not reliably emit an "unclosed database" warning on GC, so a
warning-based test would silently pass on the leaky code. Instead we track the
connections each helper opens and assert every one is closed afterward
(operating on a closed connection raises ``sqlite3.ProgrammingError``). This
fails on the pre-fix ``with sqlite3.connect(...)`` code and passes after.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from salient_core.daemon import _prompts


def _make_empty_meta_db(path: Path) -> None:
    """A DB with an empty ``meta`` table: each helper opens a connection,
    queries, finds no row, and returns its default — exercising the full open/
    close lifecycle without coupling to any value-parsing contract."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()


def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True


_HELPERS = [
    _prompts._peek_swarms,
    _prompts._peek_active_engagement,
    _prompts._peek_running_agents,
    _prompts._peek_spawned_cfgs,
]


@pytest.mark.parametrize("helper", _HELPERS, ids=lambda h: h.__name__)
def test_peek_helper_closes_its_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper
) -> None:
    db = tmp_path / "context.db"
    _make_empty_meta_db(db)  # uses real connect, before patching

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(_prompts.sqlite3, "connect", tracking_connect)

    helper(db)  # opens a read-only connection that must be closed

    assert opened, f"{helper.__name__} opened no connection to assert on"
    still_open = [c for c in opened if not _is_closed(c)]
    assert not still_open, (
        f"{helper.__name__} left {len(still_open)} sqlite connection(s) open — "
        "wrap the connect() in contextlib.closing()"
    )
