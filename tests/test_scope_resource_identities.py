import sqlite3

import pytest

from salient_core.policy.scope import (
    ResourceIdentityError,
    ScopeRuleSchemaError,
    ScopeStore,
    Target,
    parse_rule,
)


def test_existing_host_rule_round_trips_without_behavior_change(tmp_path):
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "baseline")

    rule = store.add_adhoc("Example.COM.", reason="baseline characterization")

    assert parse_rule("Example.COM.") == ("host_exact", "example.com")
    assert rule.pattern == "example.com"
    assert rule.kind == "host_exact"
    assert [(item.kind, item.pattern) for item in store.rules()] == [("host_exact", "example.com")]
    assert sqlite3.connect(db_path).execute("SELECT kind, pattern FROM scope_rules").fetchall() == [
        ("host_exact", "example.com")
    ]


def test_letter_leading_ipv6_address_and_network_keep_legacy_precedence():
    assert parse_rule("dead:beef::1") == ("network", "dead:beef::1/128")
    assert parse_rule("dead:beef::/64") == ("network", "dead:beef::/64")


def test_letter_leading_ipv6_rule_matches_address_in_scope_store():
    store = ScopeStore(None, "ipv6-resource-tag-regression")
    store.add_adhoc("dead:beef::/64", reason="IPv6 regression proof")

    assert store.check([Target("ip", "dead:beef::1", "target")]).allowed is True


@pytest.mark.parametrize(
    ("authored", "kind", "canonical"),
    [
        (
            "repo:BÜCHER.example/Team/Repo%2FOne",
            "repo",
            "repo:xn--bcher-kva.example/Team/Repo%2FOne",
        ),
        (
            "cloud:aws/aws/s3/us-east-1/123456789012/Bücket",
            "cloud",
            "cloud:aws/aws/s3/us-east-1/123456789012/B%C3%BCcket",
        ),
        (
            "cloud:azure/Tenant/Subscription/Resource%2FName",
            "cloud",
            "cloud:azure/Tenant/Subscription/Resource%2FName",
        ),
        (
            "cloud:gcp/Organization/Project/Resource",
            "cloud",
            "cloud:gcp/Organization/Project/Resource",
        ),
        (
            "saas:google/email/User/EXAMPLE.COM",
            "saas",
            "saas:google/email/user/example.com",
        ),
        (
            "saas:custom/username/CaseSensitive",
            "saas",
            "saas:custom/username/CaseSensitive",
        ),
    ],
)
def test_resource_rule_parse_and_serialize_are_idempotent(authored, kind, canonical):
    assert parse_rule(authored) == (kind, canonical)
    assert parse_rule(canonical) == (kind, canonical)


def test_resource_identity_unicode_normalizes_but_case_sensitive_segments_do_not_alias():
    composed = parse_rule("repo:github.com/Team/Café")
    decomposed = parse_rule("repo:github.com/Team/Café")

    assert composed == decomposed
    assert parse_rule("repo:github.com/Team/Repo") != parse_rule("repo:github.com/team/repo")


@pytest.mark.parametrize(
    "pattern",
    [
        "repo:github.com/acme/*",
        "repo:github.com/acme/**",
        "repo:github.com/acme/repo/extra",
        "repo:github.com//repo",
        "repo:github.com/acme/%ZZ",
        "cloud:unknown/tenant/resource",
        "cloud:aws/aws/s3/us-east-1/not-an-account/bucket",
        "cloud:azure/tenant/subscription",
        "cloud:gcp/org/project",
        "saas:github/account/octocat",
        "saas:google/email/missing-domain",
        "unknown:value",
        "Repo:github.com/acme/repository",
    ],
)
def test_malformed_wildcard_and_unknown_resource_rules_raise_typed_error(pattern):
    with pytest.raises(ResourceIdentityError):
        parse_rule(pattern)


def test_near_prefix_is_not_a_resource_family():
    with pytest.raises(ResourceIdentityError):
        parse_rule("repository:github.com/acme/repo")


def test_repo_cloud_and_saas_rules_persist_and_remove_independently(tmp_path):
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "families")
    authored = [
        "repo:github.com/acme/X",
        "cloud:gcp/org/project/X",
        "saas:github/username/X",
    ]

    for pattern in authored:
        store.add_adhoc(pattern, reason="namespace matrix")

    reloaded = ScopeStore(db_path, "families")
    assert {(rule.kind, rule.pattern) for rule in reloaded.rules()} == {
        parse_rule(pattern) for pattern in authored
    }
    assert reloaded.remove(authored[1]) is True
    assert {(rule.kind, rule.pattern) for rule in reloaded.rules()} == {
        parse_rule(authored[0]),
        parse_rule(authored[2]),
    }


def test_resource_rule_never_matches_a_different_target_family():
    store = ScopeStore(None, "namespace-match")
    rule = store.add_adhoc("repo:github.com/acme/repository", reason="namespace match")

    assert store.check([Target("repo", rule.pattern, "repo")]).allowed is True
    assert store.check([Target("cloud", rule.pattern, "cloud")]).allowed is False


def test_scope_rule_primary_key_includes_kind(tmp_path):
    db_path = tmp_path / "scope.db"
    ScopeStore(db_path, "schema")

    with sqlite3.connect(db_path) as conn:
        key_columns = [
            row[1]
            for row in sorted(
                conn.execute("PRAGMA table_info(scope_rules)"), key=lambda item: item[5]
            )
            if row[5] > 0
        ]

    assert key_columns == ["kind", "pattern", "direction", "origin"]


def test_legacy_scope_rule_schema_migrates_without_losing_rows(tmp_path):
    db_path = tmp_path / "scope.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE scope_rules (
                pattern TEXT NOT NULL, kind TEXT NOT NULL, direction TEXT NOT NULL,
                origin TEXT NOT NULL, added_by TEXT NOT NULL, added_at REAL NOT NULL,
                expires_at REAL, one_shot INTEGER NOT NULL DEFAULT 0, consumed_at REAL,
                reason TEXT NOT NULL,
                PRIMARY KEY (pattern, direction, origin)
            );
            INSERT INTO scope_rules VALUES
                ('example.com','host_exact','in','adhoc','operator',1,NULL,0,NULL,'legacy');
            """
        )

    store = ScopeStore(db_path, "migration")

    assert [(rule.kind, rule.pattern) for rule in store.rules()] == [("host_exact", "example.com")]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
        primary_key = tuple(
            row[1]
            for row in sorted(
                conn.execute("PRAGMA table_info(scope_rules)"), key=lambda item: item[5]
            )
            if row[5]
        )
        assert primary_key == ("kind", "pattern", "direction", "origin")


def test_migration_interruption_rolls_back_and_reopen_retries(tmp_path, monkeypatch):
    db_path = tmp_path / "scope.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE scope_rules (
                pattern TEXT NOT NULL, kind TEXT NOT NULL, direction TEXT NOT NULL,
                origin TEXT NOT NULL, added_by TEXT NOT NULL, added_at REAL NOT NULL,
                expires_at REAL, one_shot INTEGER NOT NULL DEFAULT 0, consumed_at REAL,
                reason TEXT NOT NULL,
                PRIMARY KEY (pattern, direction, origin)
            );
            """
        )

    import salient_core.policy._scope_schema as scope_schema_module

    original = scope_schema_module._copy_legacy_scope_rules

    def interrupted(conn):
        raise sqlite3.OperationalError("deterministic migration interruption")

    monkeypatch.setattr(scope_schema_module, "_copy_legacy_scope_rules", interrupted)
    with pytest.raises(sqlite3.OperationalError, match="deterministic migration interruption"):
        ScopeStore(db_path, "interrupted")
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables == {"scope_rules"}
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
    monkeypatch.setattr(scope_schema_module, "_copy_legacy_scope_rules", original)

    ScopeStore(db_path, "resumed")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)


def test_startup_refuses_unknown_persisted_rule_kind(tmp_path):
    db_path = tmp_path / "scope.db"
    ScopeStore(db_path, "unknown-kind")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scope_rules VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("future:x", "future", "in", "adhoc", "future", 1, None, 0, None, "future"),
        )

    with pytest.raises(ScopeRuleSchemaError, match="unknown persisted rule kind"):
        ScopeStore(db_path, "unknown-kind")


def test_startup_refuses_noncanonical_persisted_resource_identity(tmp_path):
    db_path = tmp_path / "scope.db"
    ScopeStore(db_path, "noncanonical")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scope_rules VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "repo:GitHub.com/acme/repository",
                "repo",
                "in",
                "adhoc",
                "future",
                1,
                None,
                0,
                None,
                "noncanonical",
            ),
        )

    with pytest.raises(ScopeRuleSchemaError, match="is not canonical"):
        ScopeStore(db_path, "noncanonical")


def test_startup_refuses_newer_scope_rule_schema(tmp_path):
    db_path = tmp_path / "scope.db"
    ScopeStore(db_path, "newer")
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version=3")

    with pytest.raises(ScopeRuleSchemaError, match="newer scope schema"):
        ScopeStore(db_path, "newer")
