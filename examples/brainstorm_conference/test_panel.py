"""Offline tests for the debater bench. A mock httpx transport stands in for
every provider, so these verify seat resolution + the request/parse round-trip
with no network and no API keys.

Async paths are driven with ``asyncio.run`` rather than a pytest plugin — the
repo env ships no pytest-asyncio, and an example shouldn't add a test dep.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
from panel import (  # noqa: E402
    DEFAULT_ROSTER,
    MissingSeatKeyError,
    Seat,
    _resolve_key,
    _spec_for,
    build_panelist,
    known_brains,
)


def _mock_client(capture: dict) -> httpx.AsyncClient:
    """An httpx client whose transport records the outbound payload and returns a
    canned OpenAI chat.completions response."""

    def handler(request: httpx.Request) -> httpx.Response:
        capture["url"] = str(request.url)
        capture["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "sharding beats partitioning here"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )


def test_known_brains_include_kernel_and_extra() -> None:
    brains = known_brains()
    assert {"deepseek", "glm", "minimax"} <= set(brains)  # kernel BRAIN_SPECS
    assert {"openrouter", "atlas"} <= set(brains)  # our extra specs


def test_spec_for_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown brain"):
        _spec_for("does-not-exist")


def test_resolve_key_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec_for("deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert _resolve_key(spec) is None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-abc")
    assert _resolve_key(spec) == "sk-abc"


def test_missing_key_raises_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(MissingSeatKeyError):
        build_panelist(Seat("deepseek", "deepseek"))  # no key, no injected client


def test_panelist_speaks_and_parses() -> None:
    capture: dict = {}
    panelist = build_panelist(
        Seat("ds", "deepseek", persona="Be blunt."), client=_mock_client(capture)
    )

    async def _run() -> str:
        out = await panelist.speak(
            system="You are debating.", thread="[#1 r1 glm] cache it"
        )
        await panelist.aclose()
        return out

    out = asyncio.run(_run())
    assert out == "sharding beats partitioning here"
    # persona is folded into the system message; the thread is the user message
    roles = {m["role"]: m["content"] for m in capture["payload"]["messages"]}
    assert "Be blunt." in roles["system"]
    assert "cache it" in roles["user"]
    assert capture["payload"]["model"] == "deepseek-chat"  # brain default


def test_seat_model_override_is_sent() -> None:
    capture: dict = {}
    panelist = build_panelist(
        Seat("or", "openrouter", model="anthropic/claude-3.5-sonnet"),
        client=_mock_client(capture),
    )

    async def _run() -> None:
        await panelist.speak(system="s", thread="t")
        await panelist.aclose()

    asyncio.run(_run())
    assert capture["payload"]["model"] == "anthropic/claude-3.5-sonnet"


def test_default_roster_is_three_distinct_non_claude() -> None:
    assert len(DEFAULT_ROSTER) == 3
    brains = {s.brain for s in DEFAULT_ROSTER}
    assert brains == {"deepseek", "glm", "minimax"}
    assert all(s.persona for s in DEFAULT_ROSTER)  # each voice is differentiated
