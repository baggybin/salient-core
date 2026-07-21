"""Polybrain module pins (mocked httpx + fake brains).

Pins the kernel contract the runner depends on:
- factory resolves sub-brains; missing key → clear error; registry defaults
- openai_compat parses tool calls / reasoning / usage from a mocked transport
- backend multi-turn loop: tool_call → handler invoked → second-turn text;
  events are Assistant / ToolResult / TurnCompleted with ledger-paired ids
- safeguard hook denial → handler NEVER invoked (hard-floor pin)
- interrupt terminates receive_response() promptly (no wedged quiesce)
- probe with/without keys
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import httpx
import pytest

from salient_core.polybrain import (
    MODEL_REGISTRY,
    PROVIDER_DEFAULT_MODEL,
    BrainToolCall,
    ChatMessage,
    MissingApiKeyError,
    PolybrainBackend,
    PolybrainBackendConfig,
    PolybrainProvider,
    UnknownBrainError,
    create_brain,
    find_model,
)
from salient_core.polybrain.openai_compat import BrainError, OpenAICompatBrain
from salient_core.polybrain.types import AssistantReply, Usage
from salient_core.providers import ProviderName, builtin_provider_registry
from salient_core.runtime import (
    AgentTool,
    AssistantEvent,
    JsonValue,
    ProviderErrorEvent,
    ThinkingContent,
    ToolBundle,
    ToolCallContent,
    ToolResultEvent,
    TurnCompletedEvent,
)


class FakeBrain:
    """Scripted chat-level brain."""

    name = "minimax"
    model = "MiniMax-M3"

    def __init__(self, replies: Sequence[AssistantReply] = ()) -> None:
        self._replies = list(replies)
        self.calls: list[Sequence[ChatMessage]] = []

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, JsonValue]] = (),
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AssistantReply:
        self.calls.append(messages)
        reply = self._replies.pop(0)
        return reply

    def list_models(self):
        return []

    async def aclose(self) -> None:
        pass


def _reply(
    text: str = "",
    *,
    reasoning: str | None = None,
    tool_calls: Sequence[BrainToolCall] = (),
    model: str = "MiniMax-M3",
) -> AssistantReply:
    return AssistantReply(text, reasoning, tuple(tool_calls), Usage(10, 5, 0, 15), model, "stop")


def _echo_tool(invoked: list) -> AgentTool:
    async def handler(args: Mapping[str, JsonValue]) -> JsonValue:
        invoked.append(dict(args))
        return "ok:" + str(args.get("text"))

    return AgentTool("echo", "echo tool", {"type": "object"}, handler)


def _backend(
    brain: FakeBrain,
    *,
    bundle: ToolBundle = ToolBundle(),
    safeguard_hook=None,
    max_turns: int = 25,
) -> PolybrainBackend:
    return PolybrainBackend(
        PolybrainBackendConfig(
            brain="minimax", model="MiniMax-M3", instructions="SYS", max_turns=max_turns
        ),
        brain=brain,
        tool_bundle=bundle,
        safeguard_hook=safeguard_hook,
    )


async def _drain(backend: PolybrainBackend):
    return [event async for event in backend.receive_response()]


# -- factory / registry ------------------------------------------------------


def test_create_brain_defaults(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    brain = create_brain("minimax")
    assert brain.name == "minimax"
    assert brain.model == PROVIDER_DEFAULT_MODEL["minimax"]


def test_create_brain_glm_env_fallback(monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "z")
    brain = create_brain("glm")
    assert brain.model == "glm-4-flash"


def test_create_brain_missing_key(monkeypatch):
    for env in ("MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    with pytest.raises(MissingApiKeyError, match="MINIMAX_API_KEY"):
        create_brain("minimax")


def test_create_brain_unknown():
    with pytest.raises(UnknownBrainError, match="unknown polybrain sub-brain"):
        create_brain("nope")


def test_model_registry_defaults():
    assert set(MODEL_REGISTRY) == {"minimax", "deepseek", "glm"}
    assert find_model("minimax", "minimax-m3").context_window == 200_000
    assert find_model("deepseek", "deepseek-reasoner").is_reasoning
    assert find_model("glm", "GLM-4-AIRX").context_window == 8_000
    assert find_model("glm", "nonexistent") is None


def test_builtin_registry_includes_polybrain():
    registry = builtin_provider_registry()
    provider = registry.get(ProviderName("polybrain"))
    assert isinstance(provider, PolybrainProvider)
    assert provider.capabilities.streaming is False  # honest: non-streaming v1
    assert provider.capabilities.tools is True


# -- openai_compat parsing (mocked transport) ---------------------------------


def _mock_client(payload: dict, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(
        base_url="https://api.minimax.io/v1",
        transport=httpx.MockTransport(handler),
    )


def _completion(message: dict, **extra) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "MiniMax-M3",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
        **extra,
    }


@pytest.mark.anyio
async def test_openai_compat_parses_tool_calls_and_reasoning():
    payload = _completion(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "let me think",
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                }
            ],
        }
    )
    from salient_core.polybrain.factory import get_spec

    brain = OpenAICompatBrain(
        get_spec("minimax"),
        api_key="k",
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M3",
        client=_mock_client(payload),
    )
    reply = await brain.chat(messages=[ChatMessage("user", content="hi")])
    assert reply.reasoning == "let me think"
    assert reply.tool_calls == (BrainToolCall("call_0", "echo", {"text": "hi"}),)
    assert reply.usage.total_tokens == 19
    assert reply.usage.reasoning_tokens == 3


@pytest.mark.anyio
async def test_openai_compat_reasoning_details_minimax_shape():
    payload = _completion(
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_details": [{"type": "reasoning.text", "text": "trace"}],
        }
    )
    from salient_core.polybrain.factory import get_spec

    brain = OpenAICompatBrain(
        get_spec("minimax"),
        api_key="k",
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M3",
        client=_mock_client(payload),
    )
    reply = await brain.chat(messages=[ChatMessage("user", content="hi")])
    assert reply.reasoning == "trace"
    assert reply.text == "answer"


@pytest.mark.anyio
async def test_openai_compat_bad_tool_arguments_fall_back_to_empty():
    payload = _completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{not json"},
                }
            ],
        }
    )
    from salient_core.polybrain.factory import get_spec

    brain = OpenAICompatBrain(
        get_spec("minimax"),
        api_key="k",
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M3",
        client=_mock_client(payload),
    )
    reply = await brain.chat(messages=[ChatMessage("user", content="hi")])
    assert reply.tool_calls[0].arguments == {}


@pytest.mark.anyio
async def test_openai_compat_auth_error_classified():
    from salient_core.polybrain.factory import get_spec

    brain = OpenAICompatBrain(
        get_spec("minimax"),
        api_key="bad",
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M3",
        client=_mock_client({"error": {"message": "invalid key"}}, status=401),
    )
    with pytest.raises(BrainError) as info:
        await brain.chat(messages=[ChatMessage("user", content="hi")])
    assert info.value.code == "auth"
    assert info.value.retryable is False


@pytest.mark.anyio
async def test_openai_compat_5xx_retryable():
    from salient_core.polybrain.factory import get_spec

    brain = OpenAICompatBrain(
        get_spec("minimax"),
        api_key="k",
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M3",
        client=_mock_client({"error": {"message": "boom"}}, status=500),
    )
    with pytest.raises(BrainError) as info:
        await brain.chat(messages=[ChatMessage("user", content="hi")])
    assert info.value.retryable is True


# -- backend loop --------------------------------------------------------------


@pytest.mark.anyio
async def test_backend_multi_turn_tool_loop():
    invoked: list = []
    brain = FakeBrain(
        [
            _reply(reasoning="thinking", tool_calls=[BrainToolCall("c1", "echo", {"text": "hi"})]),
            _reply("final answer"),
        ]
    )
    backend = _backend(brain, bundle=ToolBundle((_echo_tool(invoked),)))
    await backend.connect()
    await backend.query("go")
    events = await _drain(backend)

    assert invoked == [{"text": "hi"}]
    first = events[0]
    assert isinstance(first, AssistantEvent)
    assert [type(c) for c in first.content] == [ThinkingContent, ToolCallContent]
    result = events[1]
    assert isinstance(result, ToolResultEvent)
    assert result.tool_call_id == "c1"  # ledger pairing with the ToolCallContent id
    assert result.is_error is False
    last = events[-1]
    assert isinstance(last, TurnCompletedEvent)
    assert last.turns == 2
    # History carries assistant tool_calls + tool result for the next turn.
    second_call_messages = brain.calls[1]
    assert second_call_messages[0].role == "system"
    assert second_call_messages[-1].role == "tool"
    assert second_call_messages[-1].tool_call_id == "c1"
    await backend.disconnect()


@pytest.mark.anyio
async def test_backend_safeguard_denial_never_reaches_handler():
    invoked: list = []

    async def deny(name: str, args: Mapping[str, JsonValue]) -> str | None:
        return "policy says no"

    brain = FakeBrain(
        [
            _reply(tool_calls=[BrainToolCall("c1", "echo", {"text": "hi"})]),
            _reply("understood"),
        ]
    )
    backend = _backend(brain, bundle=ToolBundle((_echo_tool(invoked),)), safeguard_hook=deny)
    await backend.connect()
    await backend.query("go")
    events = await _drain(backend)

    assert invoked == []  # hard floor: denied call NEVER reaches the handler
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.is_error is True
    assert "denied by safeguard policy" in result.content
    await backend.disconnect()


@pytest.mark.anyio
async def test_backend_safeguard_failure_fails_closed():
    invoked: list = []

    async def broken(name: str, args: Mapping[str, JsonValue]) -> str | None:
        raise RuntimeError("gate exploded")

    brain = FakeBrain(
        [
            _reply(tool_calls=[BrainToolCall("c1", "echo", {"text": "hi"})]),
            _reply("ok"),
        ]
    )
    backend = _backend(brain, bundle=ToolBundle((_echo_tool(invoked),)), safeguard_hook=broken)
    await backend.connect()
    await backend.query("go")
    events = await _drain(backend)

    assert invoked == []
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.is_error is True
    assert "fail-closed" in result.content
    await backend.disconnect()


@pytest.mark.anyio
async def test_backend_unknown_tool_is_error_result():
    brain = FakeBrain(
        [
            _reply(tool_calls=[BrainToolCall("c1", "ghost", {})]),
            _reply("done"),
        ]
    )
    backend = _backend(brain)
    await backend.connect()
    await backend.query("go")
    events = await _drain(backend)
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.is_error is True
    assert "unknown tool" in result.content
    await backend.disconnect()


@pytest.mark.anyio
async def test_backend_interrupt_terminates_event_stream():
    gate = asyncio.Event()

    class SlowBrain(FakeBrain):
        async def chat(self, **kwargs) -> AssistantReply:
            await gate.wait()  # never released; interrupt must cut through
            return _reply("never")

    backend = _backend(SlowBrain())
    await backend.connect()
    await backend.query("go")
    await asyncio.sleep(0)  # let the loop task reach chat()
    await backend.interrupt()
    events = await asyncio.wait_for(_drain(backend), timeout=2)
    assert not any(isinstance(e, TurnCompletedEvent) for e in events)
    await backend.disconnect()


@pytest.mark.anyio
async def test_backend_brain_error_surfaces_provider_error():
    class FailingBrain(FakeBrain):
        async def chat(self, **kwargs) -> AssistantReply:
            raise BrainError("auth", "minimax: HTTP 401: invalid key", status=401)

    backend = _backend(FailingBrain())
    await backend.connect()
    await backend.query("go")
    events = await _drain(backend)
    error = next(e for e in events if isinstance(e, ProviderErrorEvent))
    assert error.code == "auth"
    assert error.retryable is False
    assert isinstance(events[-1], type(None)) is False  # stream closed cleanly
    await backend.disconnect()


@pytest.mark.anyio
async def test_backend_max_turns_cap():
    brain = FakeBrain(
        [_reply(tool_calls=[BrainToolCall(f"c{i}", "echo", {"text": str(i)})]) for i in range(10)]
    )
    invoked: list = []
    backend = _backend(brain, bundle=ToolBundle((_echo_tool(invoked),)), max_turns=3)
    await backend.connect()
    await backend.query("go")
    events = await _drain(backend)
    last = events[-1]
    assert isinstance(last, TurnCompletedEvent)
    assert last.turns == 3  # capped, honest count
    await backend.disconnect()


@pytest.mark.anyio
async def test_backend_context_usage_from_registry():
    brain = FakeBrain([_reply("hi")])
    backend = _backend(brain)
    await backend.connect()
    await backend.query("go")
    await _drain(backend)
    usage = await backend.get_context_usage()
    assert usage is not None
    assert usage.max_tokens == 200_000
    assert usage.used_tokens == 15
    await backend.disconnect()


# -- provider ------------------------------------------------------------------


@pytest.mark.anyio
async def test_probe_with_and_without_keys(monkeypatch):
    provider = PolybrainProvider()
    for env in ("MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    probe = await provider.probe()
    assert probe.available is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    probe = await provider.probe()
    assert probe.available is True
    assert "deepseek" in probe.detail


def test_provider_create_backend_config(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "k")
    provider = PolybrainProvider()
    backend = provider.create_backend(
        {
            "brain": "glm",
            "model": "glm-4-plus",
            "agent_name": "scout",
            "instructions": "SYS",
            "max_tokens": 4096,
        },
        tool_bundle=ToolBundle(),
    )
    assert isinstance(backend, PolybrainBackend)
    assert backend._config.brain == "glm"
    assert backend._config.model == "glm-4-plus"
    assert backend._config.agent_name == "scout"


def test_provider_create_backend_missing_key_raises(monkeypatch):
    for env in ("MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    provider = PolybrainProvider()
    with pytest.raises(MissingApiKeyError):
        provider.create_backend({"brain": "deepseek"})
