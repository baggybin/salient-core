from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio

from salient_core.policy import scope
from salient_core.policy.decision import InvocationIdentity, InvocationTransport, ToolInvocation
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.scope_evaluation import evaluate_scope
from tests.scope_relationship_fixtures import (
    compound_extractor,
    evaluate,
    external_spec,
    request,
)


def _invocation() -> ToolInvocation:
    return ToolInvocation.normalize(
        InvocationIdentity(InvocationTransport.MCP, "scan", "cloud.scan", "agent"),
        {"target": "one.example"},
    )


def _dataset() -> PolicyDataset:
    return PolicyDataset(
        tool_targets={"cloud.scan": scope.ExtractorSpec(fields={"target": "host"})},
        prohibited_patterns={},
        loud_patterns={},
    )


def _evaluate(store: scope.ScopeStore):
    return anyio.run(evaluate_scope, _invocation(), store, _dataset())


def test_audit_failure_denies_without_consuming_one_shot(tmp_path: Path) -> None:
    # Given a one-shot allow and a database failure on audit insertion.
    store = scope.ScopeStore(tmp_path / "scope.db", "audit-failure")
    store.add_adhoc("one.example", one_shot=True, reason="single")
    assert store._conn is not None
    store._conn.execute(
        "CREATE TRIGGER fail_scope_audit BEFORE INSERT ON scope_decisions "
        "BEGIN SELECT RAISE(FAIL, 'injected audit failure'); END"
    )

    # When enforcement reaches the durable authorization operation.
    result = _evaluate(store)

    # Then dispatch authority is denied and the one-shot remains usable.
    assert result.allowed is False
    assert "audit persistence failed" in result.reason
    store._conn.execute("DROP TRIGGER fail_scope_audit")
    assert _evaluate(store).allowed is True
    store.close()


def test_simultaneous_one_shot_allows_and_audits_at_most_once(tmp_path: Path) -> None:
    # Given two independent store connections racing for one authorization.
    db_path = tmp_path / "scope.db"
    creator = scope.ScopeStore(db_path, "race")
    creator.add_adhoc("one.example", one_shot=True, reason="single")
    creator.close()

    def evaluate_fresh_store(_index: int):
        store = scope.ScopeStore(db_path, "race")
        try:
            return _evaluate(store)
        finally:
            store.close()

    # When both enforce concurrently.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(evaluate_fresh_store, range(2)))

    # Then exactly one obtains authority and exactly one allow is audited.
    assert sum(result.allowed for result in results) == 1
    inspector = scope.ScopeStore(db_path, "race")
    assert inspector._conn is not None
    rows = inspector._conn.execute(
        "SELECT verdict,snapshot_id,generation,decisions_json FROM scope_decisions"
    ).fetchall()
    assert sum(row[0] == "allow" for row in rows) == 1
    assert all(row[1] and isinstance(row[2], int) for row in rows)
    assert any(json.loads(row[3]) for row in rows if row[0] == "allow")
    inspector.close()


def test_enforce_and_probe_expose_same_reconstructable_metadata(tmp_path: Path) -> None:
    # Given a durable ordinary allow rule.
    store = scope.ScopeStore(tmp_path / "scope.db", "metadata")
    store.add_adhoc("one.example", reason="ordinary")

    # When the same request is probed and enforced.
    probe = anyio.run(lambda: evaluate_scope(_invocation(), store, _dataset(), mode="probe"))
    enforce = _evaluate(store)

    # Then both name the exact snapshot and stable matched rule identity.
    assert probe.allowed is enforce.allowed is True
    assert probe.snapshot_id == enforce.snapshot_id
    assert probe.snapshot_generation == enforce.snapshot_generation
    assert probe.rule_ids == enforce.rule_ids
    assert len(enforce.rule_ids) == 1
    assert store._conn is not None
    row = store._conn.execute(
        "SELECT snapshot_id,generation,rule_ids_json,targets_json,decisions_json "
        "FROM scope_decisions"
    ).fetchone()
    assert row[:3] == (
        enforce.snapshot_id,
        enforce.snapshot_generation,
        json.dumps(list(enforce.rule_ids), separators=(",", ":")),
    )
    assert json.loads(row[3]) == [
        {"kind": "host", "source_field": "target", "value": "one.example"}
    ]
    assert json.loads(row[4])[0]["rule_id"] == enforce.rule_ids[0]
    store.close()


def test_strict_enforce_and_probe_metadata_remains_exact(tmp_path: Path) -> None:
    # Given a strict rule whose snapshot identity is known before evaluation.
    store = scope.ScopeStore(tmp_path / "scope.db", "strict-metadata-baseline")
    store.add_adhoc("one.example", reason="ordinary")
    pinned = store.snapshot

    # When the same strict request is probed and enforced.
    probe = anyio.run(lambda: evaluate_scope(_invocation(), store, _dataset(), mode="probe"))
    enforce = _evaluate(store)

    # Then the established strict metadata remains identical and exact.
    assert (probe.snapshot_id, probe.snapshot_generation, probe.rule_ids) == (
        enforce.snapshot_id,
        enforce.snapshot_generation,
        enforce.rule_ids,
    )
    assert (enforce.snapshot_id, enforce.snapshot_generation) == (
        pinned.snapshot_id,
        pinned.generation,
    )
    assert len(enforce.rule_ids) == 1
    store.close()


def test_relationship_and_snapshot_are_reconstructable_together(tmp_path: Path) -> None:
    # Given a valid compound relationship whose three identities are allowed.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = scope.ScopeStore(tmp_path / "scope.db", "relationship-audit")
    for target in (
        "aws.example",
        "saas:aws/username/profile-a",
        "cloud:aws/aws/s3/us-east-1/111111111111/bucket-a",
    ):
        store.add_adhoc(target, reason="in scope")

    try:
        # When the compound request is enforced.
        result = evaluate(external_spec(), request(), store)

        # Then its relationship facts and pinned snapshot are durable together.
        assert result.allowed is True
        assert store._conn is not None
        row = store._conn.execute(
            "SELECT snapshot_id,generation,relationships_json FROM scope_decisions"
        ).fetchone()
        relationships = json.loads(row[2])
        assert row[:2] == (result.snapshot_id, result.snapshot_generation)
        assert relationships[0]["principal_tenant"] == "tenant-a"
        assert relationships[0]["resource_tenant"] == "tenant-a"
    finally:
        store.close()
        scope.unregister_all_extractors()


def test_relationship_denial_persists_redaction_safe_relationships(tmp_path: Path) -> None:
    # Given a compound request whose principal and resource tenants conflict.
    scope.register_extractor("compound_cloud", compound_extractor)
    store = scope.ScopeStore(tmp_path / "scope.db", "relationship-denial-audit")
    raw = request()
    raw["request"]["resource_tenant"] = "tenant-c"

    try:
        # When the relationship gate denies the request before strict evaluation.
        result = evaluate(external_spec(), raw, store)

        # Then the returned and durable denial retain the same relationship facts.
        assert result.allowed is False
        assert len(result.relationships) == 1
        assert store._conn is not None
        row = store._conn.execute(
            "SELECT relationships_json,decisions_json FROM scope_decisions"
        ).fetchone()
        relationships = json.loads(row[0])
        decisions = json.loads(row[1])
        assert len(relationships) == 1
        assert relationships[0]["principal_tenant"] == "tenant-a"
        assert relationships[0]["resource_tenant"] == "tenant-c"
        assert decisions[0]["verdict"] == "deny"
        assert decisions[0]["target"] == relationships[0]["resource"]
    finally:
        store.close()
        scope.unregister_all_extractors()


def test_pre_strict_branches_expose_exact_snapshot_metadata(tmp_path: Path) -> None:
    # Given one store snapshot and representative pre-strict evaluator branches.
    store = scope.ScopeStore(tmp_path / "scope.db", "pre-strict-metadata")
    pinned = store.snapshot
    unclassified = anyio.run(
        evaluate_scope,
        _invocation(),
        store,
        PolicyDataset(tool_targets={}, prohibited_patterns={}, loud_patterns={}),
    )
    extraction_denied = anyio.run(
        evaluate_scope,
        _invocation(),
        store,
        PolicyDataset(
            tool_targets={"cloud.scan": scope.ExtractorSpec(fields={"missing": "host_required"})},
            prohibited_patterns={},
            loud_patterns={},
        ),
    )
    local = anyio.run(
        evaluate_scope,
        _invocation(),
        store,
        PolicyDataset(
            tool_targets={"cloud.scan": scope.ExtractorSpec(local_only=True)},
            prohibited_patterns={},
            loud_patterns={},
        ),
    )
    scope.register_extractor("compound_cloud", compound_extractor)
    raw = request()
    raw["request"]["resource_tenant"] = "tenant-c"

    try:
        # When a relationship denial is evaluated against that same snapshot.
        relationship_denied = evaluate(external_spec(), raw, store)

        # Then every pre-strict result names the exact pinned snapshot generation.
        for result in (unclassified, extraction_denied, local, relationship_denied):
            assert (result.snapshot_id, result.snapshot_generation) == (
                pinned.snapshot_id,
                pinned.generation,
            )
            assert result.rule_ids == ()
    finally:
        store.close()
        scope.unregister_all_extractors()


def test_pre_strict_audit_failure_returns_fail_closed_denial(tmp_path: Path) -> None:
    # Given an early selector denial whose durable audit insertion will fail.
    store = scope.ScopeStore(tmp_path / "scope.db", "pre-strict-audit-failure")
    assert store._conn is not None
    store._conn.execute(
        "CREATE TRIGGER fail_scope_audit BEFORE INSERT ON scope_decisions "
        "BEGIN SELECT RAISE(FAIL, 'injected audit failure'); END"
    )
    dataset = PolicyDataset(
        tool_targets={
            "cloud.scan": scope.ExtractorSpec(external_modes=external_spec().external_modes)
        },
        prohibited_patterns={},
        loud_patterns={},
    )

    # When evaluation reaches the selector denial.
    result = anyio.run(evaluate_scope, _invocation(), store, dataset)

    # Then the SQLite interruption is converted into an explicit denial result.
    assert result.allowed is False
    assert result.reason == "audit persistence failed; authorization denied"
    assert store._conn.execute("SELECT COUNT(*) FROM scope_decisions").fetchone()[0] == 0
    store.close()


def test_targetless_enforce_audits_once_and_probe_is_read_only(tmp_path: Path) -> None:
    # Given a targetless classification and its exact current snapshot.
    store = scope.ScopeStore(tmp_path / "scope.db", "targetless-audit")
    pinned = store.snapshot
    dataset = PolicyDataset(
        tool_targets={"cloud.scan": scope.ExtractorSpec(none=True)},
        prohibited_patterns={},
        loud_patterns={},
    )

    # When the classification is probed once and enforced once.
    probe = anyio.run(lambda: evaluate_scope(_invocation(), store, dataset, mode="probe"))
    enforce = anyio.run(evaluate_scope, _invocation(), store, dataset)

    # Then only enforce writes one complete allow row with exact snapshot metadata.
    assert probe.allowed is enforce.allowed is True
    assert (enforce.snapshot_id, enforce.snapshot_generation) == (
        pinned.snapshot_id,
        pinned.generation,
    )
    assert store._conn is not None
    rows = store._conn.execute(
        "SELECT verdict,snapshot_id,generation,targets_json,relationships_json FROM scope_decisions"
    ).fetchall()
    assert rows == [("allow", pinned.snapshot_id, pinned.generation, "[]", "[]")]
    store.close()


def test_pre_strict_enforce_and_probe_pin_latest_committed_snapshot(tmp_path: Path) -> None:
    # Given one stale store connection after another publishes a newer snapshot.
    db_path = tmp_path / "scope.db"
    stale = scope.ScopeStore(db_path, "pre-strict-stale")
    publisher = scope.ScopeStore(db_path, "pre-strict-stale")
    publisher.add_adhoc("one.example", reason="advance committed head")
    committed = publisher.snapshot
    targetless_dataset = PolicyDataset(
        tool_targets={"cloud.scan": scope.ExtractorSpec(none=True)},
        prohibited_patterns={},
        loud_patterns={},
    )

    try:
        # When stale evaluates an enforcing targetless branch and probing denial.
        enforce = anyio.run(evaluate_scope, _invocation(), stale, targetless_dataset)
        probe = anyio.run(
            lambda: evaluate_scope(
                _invocation(),
                stale,
                PolicyDataset(tool_targets={}, prohibited_patterns={}, loud_patterns={}),
                mode="probe",
            )
        )

        # Then both return the committed head and enforce audits that exact identity.
        assert (enforce.snapshot_id, enforce.snapshot_generation) == (
            committed.snapshot_id,
            committed.generation,
        )
        assert (probe.snapshot_id, probe.snapshot_generation) == (
            committed.snapshot_id,
            committed.generation,
        )
        assert stale._conn is not None
        row = stale._conn.execute("SELECT snapshot_id,generation FROM scope_decisions").fetchone()
        head = stale._conn.execute("SELECT generation FROM scope_head WHERE singleton=1").fetchone()
        assert row == (committed.snapshot_id, committed.generation)
        assert head == (committed.generation,)
    finally:
        stale.close()
        publisher.close()
