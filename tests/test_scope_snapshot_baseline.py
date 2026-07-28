from pathlib import Path

from salient_core.policy.scope import ScopeStore


def test_engagement_rules_survive_sqlite_reload(tmp_path: Path) -> None:
    # Given: a persisted store with one engagement rule.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "baseline")

    # When: the profile is loaded and the store is reconstructed from SQLite.
    store.load_engagement_rules({"scope": {"in_targets": ["example.test"]}})
    reloaded = ScopeStore(db_path, "baseline")

    # Then: the replacement remains a complete rule set after reconstruction.
    assert [rule.pattern for rule in reloaded.rules(include_inactive=True)] == ["example.test"]


def test_adhoc_rule_survives_sqlite_reload(tmp_path: Path) -> None:
    # Given: a persisted store with one adhoc rule.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "baseline")

    # When: the rule is added and the store is reconstructed from SQLite.
    added = store.add_adhoc("example.test", reason="baseline characterization")
    reloaded = ScopeStore(db_path, "baseline")

    # Then: the persisted rule retains its namespace-safe identity.
    assert [(rule.kind, rule.pattern) for rule in reloaded.rules()] == [(added.kind, added.pattern)]
