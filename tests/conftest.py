"""Shared fixtures for the salient-core kernel test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """A clean SQLite path under pytest's tmp_path."""
    return tmp_path / "test.db"
