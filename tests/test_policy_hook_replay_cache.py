from __future__ import annotations

import pytest

from salient_core.daemon._policy_hook_adapter import (
    HookReplayCache,
    ReplayOutcome,
    ReplayOwner,
    ReplayRejected,
)
from salient_core.policy.decision import ToolInvocation, sdk_identity


@pytest.mark.anyio
async def test_replay_cache_digests_secrets_and_fails_closed_at_fixed_capacity() -> None:
    # Given a cache receiving 10,001 distinct IDs with one secret raw input.
    cache = HookReplayCache()
    first = ToolInvocation.normalize(
        sdk_identity("Read", "agent"),
        {"password": "unique-raw-cache-secret"},
    )
    first_owner = await cache.reserve("id-0", first)
    assert isinstance(first_owner, ReplayOwner)
    cache.complete(first_owner, {})
    rejected = None
    for index in range(1, 10_001):
        reservation = await cache.reserve(f"id-{index}", first)
        if isinstance(reservation, ReplayOwner):
            cache.complete(reservation, {})
        else:
            rejected = reservation

    # When retained cache state and the oldest ID are inspected.
    retained = repr(tuple(entry.fingerprint for entry in cache._completed.values()))
    oldest = await cache.reserve("id-0", first)

    # Then raw input is absent, capacity is bounded, and prior IDs remain owned.
    assert "unique-raw-cache-secret" not in retained
    assert len(cache._completed) <= 10_000
    assert isinstance(rejected, ReplayRejected)
    assert "capacity" in rejected.reason
    assert isinstance(oldest, ReplayOutcome)
    assert oldest.outcome == {}
