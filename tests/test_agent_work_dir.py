"""Agent working-directory confinement (_RunnerFactoryMixin._agent_work_dir).

Builtin file tools (the SDK Bash/Write tools, codex file ops) must never
default to the daemon's launch cwd (typically the source tree). With no
engagement active, writes go to a dedicated per-agent scratch dir; with an
engagement, they co-locate under it.
"""

from __future__ import annotations

from pathlib import Path

from salient_core.daemon._runner_factory import _RunnerFactoryMixin


class _H(_RunnerFactoryMixin):
    def __init__(self, engagement_path: Path | None = None) -> None:
        self.engagement_path = engagement_path


def test_no_engagement_uses_scratch_not_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("SALIENT_AGENT_SCRATCH", str(tmp_path))
    d = _H()._agent_work_dir("osint")
    assert d == tmp_path / "osint"
    assert d.is_dir()
    assert d.resolve() != Path.cwd().resolve()
    assert (d.stat().st_mode & 0o777) == 0o700


def test_two_agents_get_distinct_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("SALIENT_AGENT_SCRATCH", str(tmp_path))
    a = _H()._agent_work_dir("osint")
    b = _H()._agent_work_dir("recon")
    assert a != b  # no cross-agent collision on identically-named artifacts


def test_engagement_path_wins(tmp_path):
    eng = tmp_path / "eng"
    eng.mkdir()
    d = _H(engagement_path=eng)._agent_work_dir("osint")
    assert d == Path(eng)


def test_name_is_sanitised_no_path_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("SALIENT_AGENT_SCRATCH", str(tmp_path))
    d = _H()._agent_work_dir("../etc/passwd")
    assert d.parent == tmp_path  # stays a single segment under the root
    assert "/" not in d.name
    assert d.is_dir()


def test_default_base_is_salient_scratch_under_home(tmp_path, monkeypatch):
    monkeypatch.delenv("SALIENT_AGENT_SCRATCH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    d = _H()._agent_work_dir("osint")
    assert d == tmp_path / ".salient" / "scratch" / "osint"
