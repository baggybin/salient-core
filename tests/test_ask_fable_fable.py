"""ask_fable Fable invocation — response shaping, CLI bridge, dispatch.

The SDK path is exercised via `run()` dispatch (with `_run_sdk` stubbed); the CLI
bridge is tested by mocking `subprocess.run`. No real model is ever called.
"""

from __future__ import annotations

import asyncio
import subprocess

import salient_core.ask_fable.fable as fable
from salient_core.ask_fable.fable import FableResult


def _run(coro):
    return asyncio.run(coro)


def test_shape_ok_refused_empty():
    assert fable._shape("The router dispatches to _handle.").status == "ok"
    r = fable._shape("REFUSED: question is too broad")
    assert r.status == "refused" and r.text == "question is too broad"
    assert fable._shape("REFUSED:").status == "refused"  # empty reason -> default
    assert fable._shape("   ").status == "error" and fable._shape("").kind == "sdk_error"


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_cli_ok_and_argv(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")
        return _fake_completed(0, "Handlers are registered in build_server().")

    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(fable.subprocess, "run", fake_run)
    res = _run(fable.run("How are handlers registered?", "def build(): ...", use_cli=True))
    assert res.status == "ok" and "Handlers" in res.text
    argv = seen["argv"]
    assert "-p" in argv and "claude-fable-5" in argv
    assert "--strict-mcp-config" in argv
    # tools disabled: the flag is present with an empty-string value
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    # question travels on stdin, not argv
    assert seen["input"].startswith("QUESTION:")
    assert not any("QUESTION" in a for a in argv)


def test_cli_refused(monkeypatch):
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        fable.subprocess, "run", lambda *a, **k: _fake_completed(0, "REFUSED: not about code")
    )
    res = _run(fable.run("what is the best language overall really", use_cli=True))
    assert res.status == "refused" and res.text == "not about code"


def test_cli_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(fable.subprocess, "run", boom)
    res = _run(fable.run("Does the parser handle nested fences here?", use_cli=True, timeout=1))
    assert res.status == "error" and res.kind == "timeout"


def test_cli_nonzero(monkeypatch):
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(fable.subprocess, "run", lambda *a, **k: _fake_completed(1, "", "kaboom"))
    res = _run(fable.run("Where is the router defined in this module?", use_cli=True))
    assert res.status == "error" and res.kind == "sdk_error" and "kaboom" in res.text


def test_cli_binary_missing(monkeypatch):
    called = {"run": False}
    monkeypatch.setattr(fable.shutil, "which", lambda b: None)
    monkeypatch.setattr(fable.subprocess, "run", lambda *a, **k: called.__setitem__("run", True))
    res = _run(fable.run("How does dispatch work in this file?", use_cli=True))
    assert res.status == "error" and res.kind == "binary_missing"
    assert called["run"] is False  # never spawned


def test_run_dispatches_to_sdk_with_resume(monkeypatch):
    seen = {}

    async def fake_sdk(message, timeout, resume):
        seen["resume"] = resume
        seen["message"] = message
        return FableResult("ok", text="ok", session_id="sid-2")

    monkeypatch.setattr(fable, "_run_sdk", fake_sdk)
    res = _run(fable.run("Trace the call path from run() to _handle.", resume="sid-1"))
    assert res.status == "ok" and res.session_id == "sid-2"
    assert seen["resume"] == "sid-1" and seen["message"].startswith("QUESTION:")


def test_run_falls_back_to_cli_on_sdk_importerror(monkeypatch):
    async def boom(*a, **k):
        raise ImportError("no sdk")

    async def fake_cli(message, timeout):
        return FableResult("ok", text="from-cli")

    monkeypatch.setattr(fable, "_run_sdk", boom)
    monkeypatch.setattr(fable, "_run_cli", fake_cli)
    res = _run(fable.run("How does the module route requests internally?"))
    assert res.status == "ok" and res.text == "from-cli"
