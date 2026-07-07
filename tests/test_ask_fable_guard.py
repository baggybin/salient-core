"""ask_fable request guard — heuristics + reused denylist, ordering.

The prohibited-use denylist (`check_prompt_intent`) is treated as a black box and
stubbed here so these tests never depend on its internal word lists.
"""

from __future__ import annotations

import salient_core.ask_fable.guard as guard


def test_denylist_rejection_uses_label(monkeypatch):
    monkeypatch.setattr(guard, "check_prompt_intent", lambda p: (False, "prohibited_x"))
    allowed, reason = guard.check("a perfectly long and specific enough question here")
    assert allowed is False
    assert reason == "prohibited_x"


def test_allowed_when_denylist_passes(monkeypatch):
    monkeypatch.setattr(guard, "check_prompt_intent", lambda p: (True, ""))
    allowed, reason = guard.check("Does run() call _load_graph before _maybe_reload in serve.py?")
    assert allowed is True
    assert reason == ""


def test_empty_and_near_empty_rejected(monkeypatch):
    # Regression: the safeguard returns (True, "") for empty input, so the
    # sanity floor must catch these independently — stub the denylist to prove
    # the floor fires first (it would have passed empty).
    monkeypatch.setattr(guard, "check_prompt_intent", lambda p: (True, ""))
    assert guard.check("")[0] is False
    assert guard.check("   ")[0] is False
    assert guard.check("hi")[1].startswith("question too short")  # 2 < 3 chars


def test_breadth_is_allowed(monkeypatch):
    # Broad engineering questions are legitimate — the heuristic must NOT reject
    # them (scope is the model contract's job, layer 3).
    monkeypatch.setattr(guard, "check_prompt_intent", lambda p: (True, ""))
    assert guard.check("how should I structure this service?")[0] is True
    assert guard.check("what's the cleanest way to do this refactor")[0] is True
    assert guard.check("tell me everything about this module")[0] is True


def test_too_long_and_context_cap(monkeypatch):
    monkeypatch.setattr(guard, "check_prompt_intent", lambda p: (True, ""))
    assert guard.check("word " * 3000)[1].startswith("question too long")
    ok_q = "How does the request router dispatch to handlers here?"
    assert guard.check(ok_q, context="x" * 20001)[1].startswith("context too large")


def test_env_threshold_override(monkeypatch):
    monkeypatch.setattr(guard, "check_prompt_intent", lambda p: (True, ""))
    monkeypatch.setenv("ASK_FABLE_MIN_LEN", "1")
    assert guard.check("go")[0] is True
