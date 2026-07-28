"""SQLite schema lifecycle for the scope rule store."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

_SCOPE_RULES_TABLE = """
CREATE TABLE {table} (
    pattern       TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    direction     TEXT    NOT NULL,
    origin        TEXT    NOT NULL,
    added_by      TEXT    NOT NULL,
    added_at      REAL    NOT NULL,
    expires_at    REAL,
    one_shot      INTEGER NOT NULL DEFAULT 0,
    consumed_at   REAL,
    reason        TEXT    NOT NULL,
    PRIMARY KEY (kind, pattern, direction, origin)
);
"""

SCHEMA = (
    _SCOPE_RULES_TABLE.format(table="IF NOT EXISTS scope_rules")
    + """

CREATE TABLE IF NOT EXISTS scope_decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    engagement_id TEXT    NOT NULL,
    agent         TEXT    NOT NULL,
    tool          TEXT    NOT NULL,
    args_json     TEXT    NOT NULL,
    targets_json  TEXT    NOT NULL,
    verdict       TEXT    NOT NULL,
    matched_rule  TEXT,
    reason        TEXT    NOT NULL,
    decisions_json TEXT   NOT NULL DEFAULT '[]',
    relationships_json TEXT NOT NULL DEFAULT '[]',
    rule_ids_json TEXT    NOT NULL DEFAULT '[]',
    snapshot_id   TEXT    NOT NULL DEFAULT '',
    generation    INTEGER NOT NULL DEFAULT 0,
    correlation_id TEXT                       -- T3.1 spine: join key for the reconstruct cross-check
);

CREATE INDEX IF NOT EXISTS scope_decisions_ts          ON scope_decisions(ts);
CREATE INDEX IF NOT EXISTS scope_decisions_verdict     ON scope_decisions(verdict);
CREATE INDEX IF NOT EXISTS scope_decisions_engagement  ON scope_decisions(engagement_id);

CREATE TABLE IF NOT EXISTS scope_snapshots (
    generation      INTEGER PRIMARY KEY,
    snapshot_id     TEXT    NOT NULL UNIQUE,
    predecessor_id  TEXT,
    payload_json    TEXT    NOT NULL,
    committed_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS scope_head (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation      INTEGER NOT NULL REFERENCES scope_snapshots(generation)
);
"""
)

_SCOPE_SCHEMA_VERSION = 2


def _migrate_scope_decisions(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scope_decisions)")}
    additions = (
        ("decisions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("relationships_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("rule_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("snapshot_id", "TEXT NOT NULL DEFAULT ''"),
        ("generation", "INTEGER NOT NULL DEFAULT 0"),
        ("correlation_id", "TEXT"),
    )
    for name, declaration in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE scope_decisions ADD COLUMN {name} {declaration}")


class ScopeRuleSchemaError(RuntimeError):
    """Persisted rule identity cannot be interpreted by this kernel."""


def _scope_rules_primary_key(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute("PRAGMA table_info(scope_rules)").fetchall()
    return tuple(row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5])


def _copy_legacy_scope_rules(conn: sqlite3.Connection) -> None:
    """Copy version-zero rows into the namespace-safe table during migration."""
    conn.execute(
        "INSERT INTO scope_rules_v1 "
        "(pattern,kind,direction,origin,added_by,added_at,expires_at,one_shot,consumed_at,reason) "
        "SELECT pattern,kind,direction,origin,added_by,added_at,expires_at,one_shot,"
        "consumed_at,reason FROM scope_rules"
    )


def _migrate_scope_rules(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_SCOPE_RULES_TABLE.format(table="scope_rules_v1"))
        _copy_legacy_scope_rules(conn)
        conn.execute("DROP TABLE scope_rules")
        conn.execute("ALTER TABLE scope_rules_v1 RENAME TO scope_rules")
        conn.execute(f"PRAGMA user_version={_SCOPE_SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise


def initialize_scope_schema(
    conn: sqlite3.Connection,
    validate_rules: Callable[[sqlite3.Connection], None],
) -> None:
    """Create or migrate the scope schema before any persisted rule is loaded."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > _SCOPE_SCHEMA_VERSION:
        raise ScopeRuleSchemaError(
            f"newer scope schema {version} is not supported (max {_SCOPE_SCHEMA_VERSION})"
        )
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scope_rules'"
    ).fetchone()
    if table_exists is None:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version={_SCOPE_SCHEMA_VERSION}")
        return
    validate_rules(conn)
    primary_key = _scope_rules_primary_key(conn)
    if primary_key == ("pattern", "direction", "origin"):
        _migrate_scope_rules(conn)
    elif primary_key != ("kind", "pattern", "direction", "origin"):
        raise ScopeRuleSchemaError(f"unrecognized scope_rules primary key {primary_key!r}")
    elif version < _SCOPE_SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={_SCOPE_SCHEMA_VERSION}")
    conn.executescript(SCHEMA)
    _migrate_scope_decisions(conn)
