"""scope.local_targets — opt-in scope-checking of local/loopback targets.

Default (flag absent/false) is byte-compatible with the historical behavior:
addresses bound to a local NIC are filtered out as operator-side
infrastructure before rule evaluation, so a loopback target never reaches
the rules. With the flag on, local addresses are ordinary targets —
default-deny unless enrolled.

`_local_addresses` is monkeypatched to {127.0.0.1} so the test box's real
NICs don't matter.
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from pathlib import Path

import pytest

import salient_core.policy.scope as scope_module
from salient_core.policy.scope import ScopeStore, Target


@pytest.fixture(autouse=True)
def _pinned_local_addresses(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        scope_module,
        "_local_addresses",
        lambda: frozenset({ipaddress.ip_address("127.0.0.1")}),
    )


def _loopback(value: str = "127.0.0.1") -> Target:
    return Target(kind="ip", value=value, source_field="host")


def test_default_filters_loopback_as_operator_side() -> None:
    """Flag off (the default): loopback is skipped, even with NO rules —
    the documented operator-side bypass, pinned so nobody 'fixes' it."""
    store = ScopeStore(None, "default-off")
    result = store.check([_loopback()])
    assert result.allowed is True
    assert "operator-side" in result.summary
    assert store.local_targets() is False


def test_flag_on_without_scope_denies_loopback() -> None:
    store = ScopeStore(None, "flag-on-empty")
    store.load_engagement_rules({"scope": {"local_targets": True}})
    assert store.local_targets() is True
    result = store.check([_loopback()])
    assert result.allowed is False
    assert "no scope set" in result.summary


def test_flag_on_enrolled_loopback_allowed_unenrolled_denied() -> None:
    store = ScopeStore(None, "flag-on")
    store.load_engagement_rules(
        {"scope": {"local_targets": True, "in_targets": ["127.0.0.1"]}}
    )
    assert store.check([_loopback()]).allowed is True
    denied = store.check([_loopback("127.0.0.9")])
    assert denied.allowed is False
    assert "not in any in-scope rule" in denied.summary


def test_flag_on_affects_only_local_addresses() -> None:
    """A NON-local private address was never operator-side — it evaluates
    against the rules under both flag settings."""
    for flag in (False, True):
        store = ScopeStore(None, f"remote-{flag}")
        store.load_engagement_rules({"scope": {"local_targets": flag}})
        result = store.check([Target(kind="ip", value="10.10.10.5", source_field="host")])
        assert result.allowed is False, flag


def test_flag_survives_checkpoint_restore_and_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "durable")
    original = store.load_engagement_rules(
        {"scope": {"local_targets": True, "in_targets": ["127.0.0.1"]}}
    )
    checkpoint = store.checkpoint()
    # A post-checkpoint publication must not strand the flag on restore.
    publication = store.prepare_engagement_rules(
        {"scope": {"local_targets": False, "in_targets": ["10.0.0.0/8"]}}
    )
    store.publish_snapshot(publication)
    assert store.local_targets() is False

    restored = store.restore_checkpoint(checkpoint, expected_current=store.snapshot)
    assert restored is original
    assert store.local_targets() is True

    # Restart recovers the flag from the persisted snapshot.
    reloaded = ScopeStore(db_path, "durable")
    assert reloaded.local_targets() is True
    assert reloaded.check([_loopback()]).allowed is True
    assert reloaded.check([_loopback("127.0.0.9")]).allowed is False


def test_flag_off_snapshots_persist_without_the_key(tmp_path: Path) -> None:
    """Backward compatibility: the flag is opt-in, so it is serialized only
    when set — snapshots written before it existed (no key) must still
    load, and flag-off snapshots keep the pre-flag wire format."""
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "compat")
    store.load_engagement_rules({"scope": {"in_targets": ["old.example"]}})
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM scope_snapshots WHERE generation="
            "(SELECT generation FROM scope_head)"
        ).fetchone()
    assert "local_targets" not in json.loads(row[0])
    reloaded = ScopeStore(db_path, "compat")
    assert reloaded.local_targets() is False


def test_flag_on_payload_round_trips_the_key(tmp_path: Path) -> None:
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "on-format")
    store.load_engagement_rules({"scope": {"local_targets": True}})
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM scope_snapshots WHERE generation="
            "(SELECT generation FROM scope_head)"
        ).fetchone()
    assert json.loads(row[0])["local_targets"] is True
