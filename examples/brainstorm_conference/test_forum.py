"""Unit tests for the append-only ForumStore — the one net-new primitive.

Pure and offline: no salient_core, no daemon, just the store. Mirrors the
consensus_panel example's self-contained-test style (sys.path fallback so the
module imports whether pytest is run from the repo root or this directory).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402
from forum import ForumEntry, ForumStore  # noqa: E402


def test_post_assigns_monotonic_seq() -> None:
    store = ForumStore()
    a = store.post("fable", "shard by tenant")
    b = store.post("deepseek", "no, partition per customer")
    c = store.post("opus", "both miss the point")
    assert (a.seq, b.seq, c.seq) == (1, 2, 3)
    assert store.count() == 3


def test_read_is_oldest_first() -> None:
    store = ForumStore()
    for i in range(5):
        store.post("agent", f"point {i}")
    seqs = [e.seq for e in store.read()]
    assert seqs == [1, 2, 3, 4, 5]


def test_since_returns_only_newer_posts() -> None:
    store = ForumStore()
    for i in range(4):
        store.post("agent", f"point {i}")
    seen = store.read()[1].seq  # pretend we last saw post #2
    fresh = store.read(since=seen)
    assert [e.seq for e in fresh] == [3, 4]
    # since past the end ⇒ nothing new
    assert store.read(since=99) == []


def test_limit_caps_the_batch() -> None:
    store = ForumStore()
    for i in range(10):
        store.post("agent", f"point {i}")
    batch = store.read(limit=3)
    assert [e.seq for e in batch] == [1, 2, 3]


def test_append_only_same_agent_and_round_does_not_clobber() -> None:
    store = ForumStore()
    store.post("fable", "first", round=1)
    store.post("fable", "second", round=1)  # same agent, same round
    texts = [e.text for e in store.read()]
    assert texts == ["first", "second"]  # both survive; nothing overwritten


def test_empty_text_is_rejected() -> None:
    store = ForumStore()
    with pytest.raises(ValueError):
        store.post("agent", "   ")


def test_text_is_stripped() -> None:
    store = ForumStore()
    entry = store.post("agent", "  padded  ")
    assert entry.text == "padded"


def test_latest_round_tracks_max() -> None:
    store = ForumStore()
    assert store.latest_round() == 0  # empty
    store.post("a", "x", round=1)
    store.post("b", "y", round=3)
    store.post("c", "z", round=2)
    assert store.latest_round() == 3


def test_transcript_renders_thread_and_empty() -> None:
    store = ForumStore()
    assert store.transcript() == "(forum empty)"
    store.post("fable", "an idea", round=1)
    line = store.transcript()
    assert "fable" in line and "an idea" in line and "#1" in line


def test_persistence_across_reopen(tmp_path: Path) -> None:
    db = str(tmp_path / "forum.db")
    store = ForumStore(db)
    store.post("fable", "durable point", round=1)
    store.close()

    reopened = ForumStore(db)
    entries = reopened.read()
    assert len(entries) == 1
    assert entries[0].text == "durable point"
    assert entries[0].round == 1
    reopened.close()


def test_entry_render_shape() -> None:
    e = ForumEntry(seq=7, round=2, agent="opus", text="hello", ts=0.0)
    assert e.render() == "[#7 r2 opus] hello"
