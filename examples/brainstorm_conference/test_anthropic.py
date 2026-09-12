"""Offline tests for the Claude seat (Anthropic Messages client + routing)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
from anthropic_compat import AnthropicBrain  # noqa: E402
from panel import MissingSeatKeyError, Seat, build_panelist, known_brains  # noqa: E402

from salient_core.polybrain.types import ChatMessage  # noqa: E402


def _mock_client(capture: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["path"] = request.url.path
        capture["headers"] = dict(request.headers)
        capture["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "opus says: shard by tenant"}]},
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )


def test_anthropic_chat_hoists_system_and_parses_text() -> None:
    capture: dict = {}
    brain = AnthropicBrain(model="claude-opus-4-8", api_key="k", client=_mock_client(capture))

    async def _run() -> str:
        reply = await brain.chat(
            messages=[
                ChatMessage(role="system", content="be terse"),
                ChatMessage(role="user", content="cache or shard?"),
            ],
            temperature=0.5,
        )
        await brain.aclose()
        return reply.text

    text = asyncio.run(_run())
    assert text == "opus says: shard by tenant"
    assert capture["path"] == "/v1/messages"
    assert capture["payload"]["system"] == "be terse"  # system hoisted out of messages
    assert [m["role"] for m in capture["payload"]["messages"]] == ["user"]
    assert capture["payload"]["model"] == "claude-opus-4-8"
    assert capture["payload"]["temperature"] == 0.5
    assert capture["headers"]["anthropic-version"]


def test_bearer_auth_style_sets_authorization_header() -> None:
    capture: dict = {}
    brain = AnthropicBrain(
        model="claude-fable-5-1", api_key="oauth-tok", auth_style="bearer",
        client=_mock_client(capture),
    )

    async def _run() -> None:
        await brain.chat(messages=[ChatMessage(role="user", content="hi")])
        await brain.aclose()

    asyncio.run(_run())
    assert capture["headers"]["authorization"] == "Bearer oauth-tok"


def test_known_brains_include_claude_seats() -> None:
    assert {"claude", "sonnet", "opus", "fable"} <= set(known_brains())


def test_build_claude_panelist_routes_to_anthropic() -> None:
    capture: dict = {}
    panelist = build_panelist(Seat("opus", "opus"), client=_mock_client(capture))

    async def _run() -> str:
        out = await panelist.speak(system="s", thread="t")
        await panelist.aclose()
        return out

    out = asyncio.run(_run())
    assert out == "opus says: shard by tenant"
    assert capture["payload"]["model"] == "claude-opus-4-8"  # brain default model


def test_claude_seat_missing_creds_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(MissingSeatKeyError):
        build_panelist(Seat("fable", "fable"))
