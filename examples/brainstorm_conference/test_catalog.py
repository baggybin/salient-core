"""Offline tests for the model catalog (the picker's data layer)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402
from catalog import FAMILIES, brain_available, catalog, models_for  # noqa: E402


def test_catalog_covers_all_families() -> None:
    families = [entry.brain for entry in catalog()]
    assert families == list(FAMILIES)
    assert set(families) == {"deepseek", "glm", "minimax", "openrouter", "atlas", "claude"}


def test_kernel_registry_supplies_builtin_brain_models() -> None:
    # deepseek/glm/minimax model lists come from the kernel's MODEL_REGISTRY
    assert len(models_for("deepseek")) >= 1
    assert all(m.id for m in models_for("glm"))


def test_claude_and_curated_models_present() -> None:
    claude_ids = {m.id for m in models_for("claude")}
    assert "claude-opus-4-8" in claude_ids and "claude-fable-5-1" in claude_ids
    assert len(models_for("openrouter")) >= 1
    assert len(models_for("atlas")) >= 1


def test_availability_tracks_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert brain_available("deepseek") is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    assert brain_available("deepseek") is True


def test_claude_availability_tracks_anthropic_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert brain_available("claude") is False
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-tok")
    assert brain_available("claude") is True
