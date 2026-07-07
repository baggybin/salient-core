"""ask_fable server handlers — gating, dispatch, sessions, audit.

Drives the module-level `_handle_ask` / `_handle_reset` with guard, fable, and
audit stubbed, so no model is called and no real audit path is written unless a
test opts in via ASK_FABLE_AUDIT_PATH.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import salient_core.ask_fable.server as server
from salient_core.ask_fable.fable import FableResult
from salient_core.ask_fable.sessions import SessionStore


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def _stub_fable(monkeypatch, result):
    async def fake_run(question, context="", *, resume=None):
        fake_run.calls.append({"question": question, "resume": resume})
        return result

    fake_run.calls = []
    monkeypatch.setattr(server.fable, "run", fake_run)
    return fake_run


def test_guard_denied_never_calls_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (False, "prohibited_x"))
    spy = _stub_fable(monkeypatch, FableResult("ok", text="should not happen"))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    out = _run(server._handle_ask(SessionStore(), {"question": "some blocked question here"}))
    assert out == {"status": "refused", "stage": "guard", "reason": "prohibited_x"}
    assert spy.calls == []  # model never invoked


def test_ok_records_session_and_resumes(monkeypatch):
    _allow(monkeypatch)
    spy = _stub_fable(
        monkeypatch, FableResult("ok", text="The router dispatches.", session_id="sid-1")
    )
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    store = SessionStore()
    out1 = _run(
        server._handle_ask(store, {"question": "How does routing work here?", "session": "s"})
    )
    assert out1["status"] == "ok" and out1["answer"] == "The router dispatches."
    assert out1["session"] == "s"
    # follow-up resumes with the captured session id
    out2 = _run(server._handle_ask(store, {"question": "And the error path?", "session": "s"}))
    assert out2["status"] == "ok"
    assert spy.calls[-1]["resume"] == "sid-1"


def test_model_refused_and_error(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    _stub_fable(monkeypatch, FableResult("refused", text="too broad"))
    out = _run(server._handle_ask(SessionStore(), {"question": "what is the best editor to use"}))
    assert out == {"status": "refused", "stage": "model", "reason": "too broad"}
    _stub_fable(
        monkeypatch, FableResult("error", kind="timeout", text="Fable timed out after 120s")
    )
    out = _run(server._handle_ask(SessionStore(), {"question": "Trace dispatch in this module."}))
    assert out["status"] == "error" and out["kind"] == "timeout"


def test_reset_flag_dumps_and_clears(monkeypatch, tmp_path):
    _allow(monkeypatch)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _stub_fable(monkeypatch, FableResult("ok", text="answer one", session_id="sid-1"))
    store = SessionStore()
    _run(server._handle_ask(store, {"question": "First question about routing.", "session": "s"}))
    # reset=true dumps the prior turn and starts fresh
    out = _run(
        server._handle_ask(
            store, {"question": "New topic about parsing.", "session": "s", "reset": True}
        )
    )
    assert out.get("reset_dump")
    assert os.path.exists(out["reset_dump"])
    # session was cleared before this turn -> no resume id was passed
    assert store.resume_id("s") == "sid-1"  # re-populated by THIS turn


def test_handle_reset_saves_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = SessionStore()
    store.record_turn("s", "q", "a", "sid-1")
    out = server._handle_reset(store, {"session": "s", "save": True})
    assert out["cleared"] is True and out["dump"] and os.path.exists(out["dump"])
    assert store.resume_id("s") is None  # gone


def test_audit_file_is_written_owner_only(monkeypatch, tmp_path):
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(log))
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (False, "prohibited_x"))
    _run(server._handle_ask(SessionStore(), {"question": "blocked question text here"}))
    assert log.exists()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["decision"] == "denied" and rec["stage"] == "guard"
    assert "question_raw" not in rec  # hashed by default
    assert len(rec["question_sha256"]) == 64


def test_build_server_and_schema():
    s = server.build_server()
    assert s.name == "ask_fable"
    assert server._ASK_SCHEMA["required"] == ["question"]
    assert "session" in server._ASK_SCHEMA["properties"]
