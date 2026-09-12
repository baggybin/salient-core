"""Offline tests for the web picker/runner: catalog, page, request validation.

The /api/run happy path needs live models, so it isn't exercised here; the
validation branches (which reject before any model call) and the catalog/page
are fully offline via Starlette's TestClient.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from web_server import (  # noqa: E402
    RunRequestError,
    _catalog_payload,
    _parse_run_request,
    app,
)


def test_catalog_payload_shape() -> None:
    families = _catalog_payload()
    brains = {f["brain"] for f in families}
    assert {"deepseek", "glm", "minimax", "openrouter", "atlas", "claude"} <= brains
    for fam in families:
        assert set(fam) == {"brain", "available", "models"}
        assert isinstance(fam["available"], bool)


def test_parse_run_request_valid() -> None:
    topic, seats, rounds, threshold = _parse_run_request(
        {
            "topic": "shard or partition?",
            "seats": [{"brain": "deepseek"}, {"brain": "glm", "model": "glm-4-air"}],
            "rounds": 4,
            "threshold": 0.7,
        }
    )
    assert topic == "shard or partition?"
    assert [s.brain for s in seats] == ["deepseek", "glm"]
    assert seats[1].model == "glm-4-air"
    assert rounds == 4 and threshold == 0.7


def test_parse_run_request_requires_topic() -> None:
    with pytest.raises(RunRequestError):
        _parse_run_request({"topic": "  ", "seats": [{"brain": "a"}, {"brain": "b"}]})


def test_parse_run_request_needs_two_seats() -> None:
    with pytest.raises(RunRequestError):
        _parse_run_request({"topic": "x", "seats": [{"brain": "deepseek"}]})


def test_home_and_catalog_endpoints() -> None:
    client = TestClient(app)
    assert client.get("/").status_code == 200
    res = client.get("/api/catalog")
    assert res.status_code == 200
    assert "families" in res.json()


def test_run_endpoint_rejects_bad_requests() -> None:
    client = TestClient(app)
    assert client.post("/api/run", json={"topic": "", "seats": []}).status_code == 400
    assert (
        client.post("/api/run", json={"topic": "x", "seats": [{"brain": "deepseek"}]}).status_code
        == 400
    )
