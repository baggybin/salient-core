from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from salient_core.daemon import _tool_registry
from salient_core.daemon._runner_factory import _RunnerFactoryMixin
from salient_core.policy.scope import ScopeStore
from salient_core.protocols import ToolBuildContext
from salient_core.providers import (
    DuplicateProviderError,
    ProviderCapabilities,
    ProviderName,
    ProviderProbe,
    ProviderRegistry,
    UnknownProviderError,
    builtin_provider_registry,
    reset_provider_registry,
    set_provider_registry,
)
from salient_core.runtime import (
    AgentTool,
    AssistantEvent,
    JsonValue,
    NativeActionKind,
    NativeActionStartedEvent,
    TextContent,
    ToolBundle,
    TurnCompletedEvent,
    TurnUsage,
)


class _Provider:
    name = ProviderName("fake")
    capabilities = ProviderCapabilities(
        streaming=True,
        tools=True,
        interruption=True,
        context_usage=True,
    )

    async def probe(self) -> ProviderProbe:
        return ProviderProbe(available=True, detail="ready")

    def create_backend(
        self,
        config: Mapping[str, JsonValue],
        *,
        tool_bundle: ToolBundle = ToolBundle(),
    ) -> _Backend:
        del config
        return _Backend(tool_bundle)


class _Backend:
    def __init__(self, tool_bundle: ToolBundle = ToolBundle()) -> None:
        self.tool_bundle = tool_bundle

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        del prompt

    async def receive_response(self) -> AsyncIterator[AssistantEvent | TurnCompletedEvent]:
        yield AssistantEvent(content=(TextContent("hello"),), model="fake-model")
        yield TurnCompletedEvent(
            turns=1,
            duration_ms=2,
            usage=TurnUsage(input_tokens=3, output_tokens=4),
        )

    async def interrupt(self) -> None:
        return None

    async def get_context_usage(self) -> None:
        return None

    def diagnose_failure(
        self,
        agent_name: str,
        error: BaseException,
        stderr_tail: tuple[str, ...],
    ) -> str:
        del stderr_tail
        return f"{agent_name}: {type(error).__name__}: {error}"


async def _echo(arguments: Mapping[str, JsonValue]) -> JsonValue:
    return arguments.get("text", "")


def test_tool_bundle_is_immutable_and_selects_least_privilege_catalog() -> None:
    # Given: two immutable provider-neutral tools.
    first = AgentTool(
        name="echo",
        description="Echo text",
        input_schema={"type": "object"},
        handler=_echo,
    )
    second = AgentTool(
        name="other",
        description="Other",
        input_schema={"type": "object"},
        handler=_echo,
    )

    # When: a least-privilege subset is selected.
    selected = ToolBundle((first, second)).select(frozenset({"echo"}))

    # Then: only the permitted tool remains and frozen values reject mutation.
    assert selected.tools == (first,)
    assert AgentTool.__dataclass_params__.frozen is True
    assert ToolBundle.__dataclass_params__.frozen is True


def test_sequence_fields_do_not_alias_mutable_constructor_lists() -> None:
    tool = AgentTool("echo", "Echo text", {"type": "object"}, _echo)
    tools = [tool]
    content = [TextContent("first")]

    bundle = ToolBundle(tools)
    event = AssistantEvent(content, "fake-model")
    tools.clear()
    content.append(TextContent("second"))

    assert bundle.tools == (tool,)
    assert event.content == (TextContent("first"),)


def test_tool_build_context_does_not_alias_mutable_constructor_lists() -> None:
    tool = AgentTool("echo", "Echo text", {"type": "object"}, _echo)
    tools = [tool]
    wires = ["echo"]

    context = ToolBuildContext(None, None, "agent", tools, wires)
    tools.clear()
    wires.clear()

    assert context.extra_tools == (tool,)
    assert context.extra_bare_wires == ("echo",)


def test_agent_tool_deeply_freezes_json_schema() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }

    tool = AgentTool("echo", "Echo text", schema, _echo)
    nested = schema["properties"]
    assert isinstance(nested, dict)
    nested["text"] = {"type": "number"}

    assert tool.input_schema["properties"] != schema["properties"]


def test_native_action_event_deeply_freezes_arguments() -> None:
    arguments: dict[str, JsonValue] = {"command": ["printf", "ok"]}

    event = NativeActionStartedEvent(
        "action-1",
        NativeActionKind.COMMAND,
        "shell",
        arguments,
    )
    command = arguments["command"]
    assert isinstance(command, list)
    command.append("changed")

    assert event.arguments["command"] == ("printf", "ok")


def test_registry_rejects_duplicate_and_unknown_provider_names() -> None:
    # Given: a registry containing one provider.
    registry = ProviderRegistry((_Provider(),))

    # When/Then: duplicate registration and unknown lookup fail explicitly.
    with pytest.raises(DuplicateProviderError, match="fake"):
        registry.register(_Provider())
    with pytest.raises(UnknownProviderError, match="missing"):
        registry.get(ProviderName("missing"))


def test_registry_rejects_malformed_provider_name() -> None:
    # Given/When/Then: names are parsed at the registry boundary.
    with pytest.raises(ValueError, match="provider name"):
        ProviderName("Not Valid!")


def test_registry_loads_entry_points_and_rejects_duplicate_names(monkeypatch) -> None:
    @dataclass(frozen=True, slots=True)
    class EntryPoint:
        name: str
        value: str
        provider: _Provider

        def load(self) -> _Provider:
            return self.provider

    entry_points = (
        EntryPoint("first", "package:first", _Provider()),
        EntryPoint("second", "package:second", _Provider()),
    )
    monkeypatch.setattr(
        "salient_core.providers.metadata.entry_points",
        lambda *, group: entry_points,
    )

    registry = ProviderRegistry()

    with pytest.raises(DuplicateProviderError, match="fake"):
        registry.load_entry_points()


def test_builtin_registry_creates_claude_backend_and_parses_config() -> None:
    registry = builtin_provider_registry()
    provider = registry.get(ProviderName("claude"))

    backend = provider.create_backend({"model": "claude-test"})

    assert backend.__class__.__name__ == "LocalClaudeBackend"
    with pytest.raises(ValueError, match="unknown fields"):
        provider.create_backend({"unexpected": True})


def test_claude_provider_rejects_tool_bundle_instead_of_dropping_it() -> None:
    registry = builtin_provider_registry()
    provider = registry.get(ProviderName("claude"))

    tools = ToolBundle((AgentTool("echo", "echo", {"type": "object"}, _echo),))
    with pytest.raises(ValueError, match="does not accept a provider tool bundle"):
        provider.create_backend({"model": "claude-test"}, tool_bundle=tools)
    # An empty bundle (the default) still builds a backend.
    assert (
        provider.create_backend({"model": "claude-test"}).__class__.__name__ == "LocalClaudeBackend"
    )


class _FactoryHarness(_RunnerFactoryMixin):
    def __init__(self) -> None:
        self.prompt_timeout = 60.0
        self.idle_timeout = 0.0
        self.tail_buffer_size = 100
        self.context = None
        self.profile: dict[str, Any] = {}
        self.event_hub = None
        self.engagement_path = None
        self.scope = ScopeStore(None, "test")
        self.actions = None
        self.listeners = None
        self.claude_options_built = 0

    def _build_options(self, cfg, *, stderr_callback=None):
        del cfg, stderr_callback
        self.claude_options_built += 1
        return ClaudeAgentOptions()


def test_runner_factory_selects_registered_provider_from_runtime_block(monkeypatch) -> None:
    bus_tool = AgentTool("ask_agent", "Delegate", {"type": "object"}, _echo)
    primary_tool = AgentTool("scan", "Scan", {"type": "object"}, _echo)
    monkeypatch.setattr(
        "salient_core.daemon._runner_factory.make_bus_tool_bundle",
        lambda daemon, owner: (ToolBundle((bus_tool,)), ("ask_agent",)),
    )
    observed: list[ToolBuildContext] = []

    def build_bundle(tool_type, config, *, context):
        del tool_type, config
        observed.append(context)
        return ToolBundle((primary_tool, *context.extra_tools))

    _tool_registry.set_tool_bundle_builder(build_bundle)
    set_provider_registry(ProviderRegistry((_Provider(),)))
    try:
        daemon = _FactoryHarness()

        runner = daemon._make_runner(
            {
                "name": "provider-agent",
                "runtime": {"provider": "fake", "config": {"mode": "test"}},
                "tool": {"type": "scanner", "config": {}},
            }
        )

        backend = runner._create_backend()
        assert isinstance(backend, _Backend)
        assert backend.tool_bundle is runner.tool_bundle
        assert daemon.claude_options_built == 0
        assert tuple(tool.name for tool in runner.tool_bundle.tools) == ("scan", "ask_agent")
        assert observed[0].scope_store is daemon.scope
        assert observed[0].agent_name == "provider-agent"
        assert observed[0].extra_bare_wires == ("ask_agent",)
    finally:
        _tool_registry.reset()
        reset_provider_registry()


def test_runner_factory_defaults_to_claude_when_runtime_is_missing() -> None:
    reset_provider_registry()
    daemon = _FactoryHarness()

    runner = daemon._make_runner({"name": "claude-agent"})

    assert runner._create_backend().__class__.__name__ == "LocalClaudeBackend"
    assert daemon.claude_options_built == 1


def test_runner_factory_accepts_explicit_claude_runtime_context(monkeypatch) -> None:
    monkeypatch.setattr(
        "salient_core.daemon._runner_factory.make_bus_tool_bundle",
        lambda daemon, owner: (ToolBundle(), ()),
    )
    reset_provider_registry()
    daemon = _FactoryHarness()

    runner = daemon._make_runner(
        {
            "name": "explicit-claude",
            "model": "claude-test",
            "runtime": {"provider": "claude", "config": {}},
        }
    )

    assert runner._create_backend().__class__.__name__ == "LocalClaudeBackend"
    assert daemon.claude_options_built == 0


def test_runner_factory_rejects_unknown_explicit_claude_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "salient_core.daemon._runner_factory.make_bus_tool_bundle",
        lambda daemon, owner: (ToolBundle(), ()),
    )
    reset_provider_registry()
    daemon = _FactoryHarness()
    runner = daemon._make_runner(
        {
            "name": "explicit-claude",
            "runtime": {"provider": "claude", "config": {"unexpected": True}},
        }
    )

    with pytest.raises(ValueError, match="unknown fields: unexpected"):
        runner._create_backend()
