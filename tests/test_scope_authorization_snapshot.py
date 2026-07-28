import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import salient_core.policy.scope as scope_module
from salient_core.policy.scope import (
    ScopeSnapshotCompatibilityError,
    ScopeSnapshotStaleError,
    ScopeStore,
    Target,
)


def _profile(*targets: str) -> dict[str, dict[str, object]]:
    return {"scope": {"in_targets": list(targets), "session_strict": True}}


def _rewrite_head_payload(db_path: Path, mutate) -> None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT generation,payload_json FROM scope_snapshots "
            "WHERE generation=(SELECT generation FROM scope_head)"
        ).fetchone()
        payload = json.loads(row[1])
        mutate(payload)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        snapshot_id = hashlib.sha256(payload_json.encode()).hexdigest()
        conn.execute(
            "UPDATE scope_snapshots SET snapshot_id=?,payload_json=? WHERE generation=?",
            (snapshot_id, payload_json, row[0]),
        )


def test_snapshot_publication_is_durable_before_memory(tmp_path: Path) -> None:
    # Given: a store whose publication boundary is interrupted after commit.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "atomic")
    old = store.snapshot
    store._snapshot_fault = "after_commit"

    # When: a complete replacement commits but cannot publish in memory.
    with pytest.raises(RuntimeError, match="after_commit"):
        store.load_engagement_rules(_profile("new.example"))

    # Then: the old reader remains pinned and restart recovers the new snapshot.
    assert store.snapshot is old
    recovered = ScopeStore(db_path, "atomic")
    assert recovered.snapshot.generation == old.generation + 1
    assert [rule.pattern for rule in recovered.snapshot.rules] == ["new.example"]
    assert recovered.snapshot.rule_ids == ScopeStore(db_path, "atomic").snapshot.rule_ids


def test_snapshot_failure_before_commit_preserves_old_state(tmp_path: Path) -> None:
    # Given: a store whose transaction boundary fails before commit.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "atomic")
    old = store.snapshot
    store._snapshot_fault = "before_commit"

    # When: publication is interrupted.
    with pytest.raises(RuntimeError, match="before_commit"):
        store.load_engagement_rules(_profile("new.example"))

    # Then: both memory and restart retain the complete predecessor.
    assert store.snapshot is old
    assert ScopeStore(db_path, "atomic").snapshot.snapshot_id == old.snapshot_id


def test_checkpoint_restore_rewinds_post_commit_interruption_exactly(tmp_path: Path) -> None:
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "rollback")
    original = store.load_engagement_rules(_profile("old.example"))
    checkpoint = store.checkpoint()
    publication = store.prepare_engagement_rules(_profile("new.example"))
    store._snapshot_fault = "after_commit"

    with pytest.raises(RuntimeError, match="after_commit"):
        store.publish_snapshot(publication)

    store._snapshot_fault = None
    restored = store.restore_checkpoint(checkpoint, expected_current=publication)

    assert restored is original
    assert store.snapshot is original
    reloaded = ScopeStore(db_path, "rollback")
    assert reloaded.snapshot == original
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT generation FROM scope_head").fetchone() == (
            original.generation,
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM scope_snapshots WHERE generation > ?",
            (original.generation,),
        ).fetchone() == (0,)
        assert conn.execute("SELECT pattern FROM scope_rules ORDER BY rowid").fetchall() == [
            ("old.example",)
        ]


def test_interrupt_during_commit_rolls_back_and_leaves_store_usable(tmp_path: Path) -> None:
    # A KeyboardInterrupt landing inside the publish transaction (before COMMIT)
    # must roll back the open transaction and re-raise — never leave a dangling
    # transaction that wedges every later BEGIN IMMEDIATE.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "interrupt-commit")
    original = store.load_engagement_rules(_profile("old.example"))
    publication = store.prepare_engagement_rules(_profile("new.example"))

    real_insert = store._insert_snapshot_row

    def interrupt(snapshot):
        real_insert(snapshot)
        raise KeyboardInterrupt

    store._insert_snapshot_row = interrupt
    with pytest.raises(KeyboardInterrupt):
        store.publish_snapshot(publication)

    # No dangling transaction, and the old snapshot is preserved in memory.
    assert store._conn is not None
    assert store._conn.in_transaction is False
    assert store.snapshot is original
    # The store is not wedged — a subsequent publication still commits.
    store._insert_snapshot_row = real_insert
    recovered = store.load_engagement_rules(_profile("recover.example"))
    assert [rule.pattern for rule in recovered.rules] == ["recover.example"]


def test_interrupt_during_checkpoint_restore_leaves_store_usable(tmp_path: Path) -> None:
    # Same guarantee on the checkpoint-rewind transaction.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "interrupt-restore")
    store.load_engagement_rules(_profile("old.example"))
    checkpoint = store.checkpoint()
    publication = store.prepare_engagement_rules(_profile("new.example"))
    store.publish_snapshot(publication)  # head now at the new generation

    real_insert_rule = store._insert_rule_row

    def interrupt(rule):
        raise KeyboardInterrupt

    store._insert_rule_row = interrupt
    with pytest.raises(KeyboardInterrupt):
        store.restore_checkpoint(checkpoint, expected_current=publication)

    # The demotion transaction rolled back; nothing is wedged and the durable
    # head still names the published generation (the rewind committed nothing).
    assert store._conn is not None
    assert store._conn.in_transaction is False
    store._insert_rule_row = real_insert_rule
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT generation FROM scope_head").fetchone() == (
            publication.generation,
        )
    assert store.checkpoint() is not None


def test_checkpoint_restore_republishes_original_in_memory_snapshot() -> None:
    store = ScopeStore(None, "memory-rollback")
    original = store.load_engagement_rules(_profile("old.example"))
    checkpoint = store.checkpoint()
    publication = store.prepare_engagement_rules(_profile("new.example"))
    store.publish_snapshot(publication)

    assert store.restore_checkpoint(checkpoint, expected_current=publication) is original
    assert store.snapshot is original


def test_checkpoint_restore_refuses_to_erase_concurrent_publication(tmp_path: Path) -> None:
    db_path = tmp_path / "scope.db"
    owner = ScopeStore(db_path, "rollback-cas")
    owner.load_engagement_rules(_profile("old.example"))
    checkpoint = owner.checkpoint()
    owned = owner.prepare_engagement_rules(_profile("owned.example"))
    owner.publish_snapshot(owned)
    concurrent = ScopeStore(db_path, "rollback-cas")
    newer = concurrent.load_engagement_rules(_profile("newer.example"))

    with pytest.raises(ScopeSnapshotStaleError):
        owner.restore_checkpoint(checkpoint, expected_current=owned)

    assert ScopeStore(db_path, "rollback-cas").snapshot == newer


def test_checkpoint_restore_refuses_when_concurrent_writer_wins_first(tmp_path: Path) -> None:
    db_path = tmp_path / "scope.db"
    owner = ScopeStore(db_path, "rollback-cas-first")
    owner.load_engagement_rules(_profile("old.example"))
    checkpoint = owner.checkpoint()
    owned = owner.prepare_engagement_rules(_profile("owned.example"))
    concurrent = ScopeStore(db_path, "rollback-cas-first")
    newer = concurrent.load_engagement_rules(_profile("newer.example"))

    with pytest.raises(ScopeSnapshotStaleError):
        owner.publish_snapshot(owned)
    with pytest.raises(ScopeSnapshotStaleError):
        owner.restore_checkpoint(checkpoint, expected_current=owned)

    assert ScopeStore(db_path, "rollback-cas-first").snapshot == newer


def test_snapshot_compare_and_swap_rejects_stale_writer(tmp_path: Path) -> None:
    # Given: two writers pinned to the same generation.
    db_path = tmp_path / "scope.db"
    first = ScopeStore(db_path, "cas")
    stale = ScopeStore(db_path, "cas")
    expected = first.snapshot.generation

    # When: one writer publishes before the other.
    first.load_engagement_rules(_profile("first.example"), expected_generation=expected)

    # Then: the stale writer cannot replace the committed head.
    with pytest.raises(ScopeSnapshotStaleError):
        stale.load_engagement_rules(_profile("stale.example"), expected_generation=expected)


def test_restore_creates_forward_generation(tmp_path: Path) -> None:
    # Given: two distinct committed snapshots.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "restore")
    original = store.load_engagement_rules(_profile("one.example"))
    current = store.load_engagement_rules(_profile("two.example"))

    # When: the original content is restored.
    restored = store.restore_snapshot(original.snapshot_id)

    # Then: restore is a new generation with current as its predecessor.
    assert restored.generation == current.generation + 1
    assert restored.predecessor_id == current.snapshot_id
    assert [rule.pattern for rule in restored.rules] == ["one.example"]
    assert restored.snapshot_id != original.snapshot_id


def test_startup_rejects_incompatible_snapshot_identity(tmp_path: Path) -> None:
    # Given: a committed snapshot whose persisted policy identity is malformed.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "compatibility")
    store.load_engagement_rules(_profile("one.example"))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scope_snapshots SET payload_json=replace("
            'payload_json, \'"policy":"scope-policy-v1"\', '
            '\'"policy":"unknown"\') WHERE generation=(SELECT generation FROM scope_head)'
        )

    # When/Then: startup fails closed instead of publishing reinterpreted state.
    with pytest.raises(ScopeSnapshotCompatibilityError):
        ScopeStore(db_path, "compatibility")


def test_evaluation_pins_one_snapshot_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an evaluation starts with two targets allowed by one snapshot.
    store = ScopeStore(tmp_path / "scope.db", "reader-pin")
    store.load_engagement_rules(_profile("one.example", "two.example"))
    pinned = store.snapshot
    original_match = scope_module._rule_matches
    published = False

    def publish_during_match(rule, target):
        nonlocal published
        if not published:
            published = True
            store.load_engagement_rules(_profile("replacement.example"))
        return original_match(rule, target)

    monkeypatch.setattr(scope_module, "_rule_matches", publish_during_match)

    # When: a new snapshot publishes during the first rule match.
    result = store.dry_check(
        [
            Target(kind="host", value="one.example", source_field="host"),
            Target(kind="host", value="two.example", source_field="host"),
        ]
    )

    # Then: the evaluation uses only the pinned predecessor while later readers see new state.
    assert result.allowed is True
    assert pinned.generation + 1 == store.snapshot.generation
    assert [rule.pattern for rule in store.snapshot.rules] == ["replacement.example"]


def test_startup_rejects_mutated_persisted_rule_id(tmp_path: Path) -> None:
    # Given: a valid persisted snapshot whose stable rule identity is changed in place.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "rule-id-integrity")
    store.load_engagement_rules(_profile("one.example"))
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT generation,payload_json FROM scope_snapshots "
            "WHERE generation=(SELECT generation FROM scope_head)"
        ).fetchone()
        payload = json.loads(row[1])
        payload["rules"][0]["rule_id"] = "0" * 64
        conn.execute(
            "UPDATE scope_snapshots SET payload_json=? WHERE generation=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), row[0]),
        )

    # When/Then: startup authenticates the stored representation and fails closed.
    with pytest.raises(ScopeSnapshotCompatibilityError):
        ScopeStore(db_path, "rule-id-integrity")


def test_snapshot_reload_preserves_authored_rule_order_and_first_match(tmp_path: Path) -> None:
    # Given: overlapping rules whose authored order determines the matched rule.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "ordered-rules")
    live = store.load_engagement_rules(_profile("*.example.com", "a.example.com"))
    target = Target(kind="host", value="a.example.com", source_field="host")
    live_match = store.dry_check([target]).decisions[0].matched_rule

    # When: the exact same snapshot is reconstructed from SQLite.
    reloaded = ScopeStore(db_path, "ordered-rules")
    reloaded_match = reloaded.dry_check([target]).decisions[0].matched_rule

    # Then: snapshot identity, order, and first-match semantics are identical.
    assert reloaded.snapshot.snapshot_id == live.snapshot_id
    assert [rule.pattern for rule in reloaded.snapshot.rules] == [
        "*.example.com",
        "a.example.com",
    ]
    assert live_match is not None
    assert reloaded_match is not None
    assert reloaded_match.pattern == live_match.pattern == "*.example.com"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["research"].update({"mode": "unknown"}),
        lambda payload: payload.update({"session_strict": "yes"}),
    ],
)
def test_startup_rejects_digest_consistent_invalid_semantic_types(tmp_path: Path, mutate) -> None:
    # Given: malformed semantic content paired with its internally consistent digest.
    db_path = tmp_path / "scope.db"
    ScopeStore(db_path, "typed-boundary")
    _rewrite_head_payload(db_path, mutate)

    # When/Then: typed parsing rejects it before publication.
    with pytest.raises(ScopeSnapshotCompatibilityError):
        ScopeStore(db_path, "typed-boundary")


def test_secret_bearing_credential_binding_is_never_persisted_or_exposed(tmp_path: Path) -> None:
    # Given: caller input containing credential secret material.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "secret-boundary")
    sentinel = "SENSITIVE_SENTINEL"

    # When: the profile attempts to place a token in authorization state.
    with pytest.raises(ScopeSnapshotCompatibilityError):
        store.load_engagement_rules(
            {"scope": {"credential_bindings": {"cloud": {"token": sentinel}}}}
        )

    # Then: neither immutable public state nor durable history contains the secret.
    assert not hasattr(store.snapshot, "credential_bindings_json")
    with sqlite3.connect(db_path) as conn:
        payloads = "".join(
            row[0] for row in conn.execute("SELECT payload_json FROM scope_snapshots")
        )
    assert sentinel not in payloads


def test_safe_opaque_credential_binding_metadata_survives_reload(tmp_path: Path) -> None:
    # Given: explicit non-secret identity and configuration metadata.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "binding-metadata")
    binding = {
        "aws-prod": {
            "provider": "aws",
            "principal": "role/readonly",
            "tenant": "123456789012",
            "configuration_id": "config-generation-7",
        }
    }

    # When: the metadata is published and reconstructed.
    published = store.load_engagement_rules(
        {"scope": {"credential_bindings": binding, "in_targets": ["one.example"]}}
    )
    reloaded = ScopeStore(db_path, "binding-metadata").snapshot

    # Then: only the typed opaque metadata is exposed with identical identity.
    assert reloaded.snapshot_id == published.snapshot_id
    assert len(reloaded.credential_bindings) == 1
    assert reloaded.credential_bindings[0].binding_id == "aws-prod"
    assert reloaded.credential_bindings[0].configuration_id == "config-generation-7"


@pytest.mark.parametrize("binding_id", [7, True, None, b"bytes"])
def test_non_string_credential_binding_id_rejects_without_changing_head(
    tmp_path: Path, binding_id
) -> None:
    # Given: a restartable last-good snapshot and otherwise valid binding metadata.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "binding-id-boundary")
    last_good = store.load_engagement_rules(_profile("one.example"))
    metadata = {
        "provider": "aws",
        "principal": "p",
        "tenant": "t",
        "configuration_id": "c",
    }

    # When: a non-string binding identity crosses the profile boundary.
    with pytest.raises(ScopeSnapshotCompatibilityError):
        store.load_engagement_rules({"scope": {"credential_bindings": {binding_id: metadata}}})

    # Then: no commit occurs and the last-good head restarts identically.
    assert store.snapshot.snapshot_id == last_good.snapshot_id
    restarted = ScopeStore(db_path, "binding-id-boundary").snapshot
    assert restarted.snapshot_id == last_good.snapshot_id
    assert restarted.generation == last_good.generation


@pytest.mark.parametrize(
    "control",
    ["\u007f", "\u0085", "\u200d", "\ue000", "\ud800", "\u0378"],
)
def test_unicode_category_c_metadata_rejects_without_changing_head(
    tmp_path: Path, control: str
) -> None:
    # Given: a valid restartable binding snapshot.
    db_path = tmp_path / "scope.db"
    store = ScopeStore(db_path, "unicode-control-boundary")
    valid = {
        "aws-prod": {
            "provider": "aws",
            "principal": "rôle/只读",
            "tenant": "t",
            "configuration_id": "c",
        }
    }
    last_good = store.load_engagement_rules({"scope": {"credential_bindings": valid}})
    malformed = {"aws-prod": {**valid["aws-prod"], "principal": f"p{control}x"}}

    # When: control, format, surrogate, private-use, or unassigned text is supplied.
    with pytest.raises(ScopeSnapshotCompatibilityError):
        store.load_engagement_rules({"scope": {"credential_bindings": malformed}})

    # Then: safe graphic Unicode remains authoritative and restart-consistent.
    assert store.snapshot.snapshot_id == last_good.snapshot_id
    restarted = ScopeStore(db_path, "unicode-control-boundary").snapshot
    assert restarted.snapshot_id == last_good.snapshot_id
    assert restarted.credential_bindings[0].principal == "rôle/只读"


def test_one_shot_consumption_uses_the_evaluations_pinned_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a one-shot evaluation pinned before a concurrent publication.
    store = ScopeStore(tmp_path / "scope.db", "one-shot-pin")
    store.add_adhoc("one.example", one_shot=True, reason="pin regression")
    original_check = store._check_one
    published = False

    def publish_after_match(target, rules):
        nonlocal published
        decision = original_check(target, rules)
        if not published:
            published = True
            store.remove("one.example")
        return decision

    monkeypatch.setattr(store, "_check_one", publish_after_match)

    # When/Then: consumption CASes the pinned generation and rejects mixed state.
    with pytest.raises(ScopeSnapshotStaleError):
        store.check([Target(kind="host", value="one.example", source_field="host")])
    assert store.rules() == []
