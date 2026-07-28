"""`gate_tool_bundle` works with NO daemon and NO mixin.

The provider-runtime gate was reachable only as
`AgentRunnerFactory._gate_provider_bundle`. That is fine for the daemon that
composes the mixin — and useless to any other host. `salient-tutor` runs its own
`TutorDaemon` with its own `_make_runner`, builds its own bundle via
`make_bus_tool_bundle`, and calls `provider.create_backend(...)` directly, so it
never touches that method: it would reimplement the same seam, and the same bug,
a third time.

So the wrapping now lives in `runtime.gate_tool_bundle`, taking its checks as a
parameter. These pins hold the property that matters for reuse — it needs
nothing but a bundle and a list of async callables — plus a delegation pin so
the mixin and the function cannot drift apart.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from salient_core.runtime import (
    POLICY_GATE_ANNOTATION,
    POLICY_GATE_BUDGET_ANNOTATION,
    AgentTool,
    PolicyDenied,
    ToolBundle,
    gate_tool_bundle,
)


def _tool(name: str, calls: list[dict[str, Any]]) -> AgentTool:
    async def handler(args):
        calls.append(dict(args))
        return {"ran": True}

    return AgentTool(name, "a tool", {"type": "object"}, handler)


def _deny(reason: str):
    async def check(_input, _tool_use_id, _ctx):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return check


def _allow():
    async def check(_input, _tool_use_id, _ctx):
        return {}

    return check


def _record(seen: list[dict[str, Any]]):
    async def check(input_data, tool_use_id, _ctx):
        seen.append({**input_data, "tool_use_id": tool_use_id})
        return {}

    return check


def _rewrite(new_input: dict[str, Any]):
    async def check(_input, _tool_use_id, _ctx):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": new_input,
            }
        }

    return check


def _gated(tools, checks, **kw) -> ToolBundle:
    return gate_tool_bundle(
        ToolBundle(tuple(tools)),
        agent_name=kw.pop("agent_name", "agent"),
        server=kw.pop("server", "agent"),
        checks=checks,
        **kw,
    )


# ---------------------------------------------------------------------------
# usable with nothing but a bundle and some callables
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_denies_without_any_daemon_present() -> None:
    calls: list[dict[str, Any]] = []
    bundle = _gated([_tool("scan", calls)], [_deny("nope")])

    with pytest.raises(PolicyDenied) as caught:
        await bundle.tools[0].handler({"target": "10.0.0.1"})

    assert str(caught.value) == "nope"
    assert caught.value.tool == "scan"
    assert caught.value.agent == "agent"
    assert calls == []


@pytest.mark.anyio
async def test_allows_and_passes_arguments_through() -> None:
    calls: list[dict[str, Any]] = []
    bundle = _gated([_tool("scan", calls)], [_allow()])

    payload = {"target": "10.0.0.1", "flags": ["-sV"]}
    assert await bundle.tools[0].handler(payload) == {"ran": True}
    assert calls == [payload]


@pytest.mark.anyio
async def test_an_empty_check_list_is_a_pass_through() -> None:
    calls: list[dict[str, Any]] = []
    bundle = _gated([_tool("scan", calls)], [])

    assert await bundle.tools[0].handler({"a": 1}) == {"ran": True}
    assert calls == [{"a": 1}]


def test_empty_bundle_is_returned_identically() -> None:
    empty = ToolBundle()
    assert gate_tool_bundle(empty, agent_name="a", server="a", checks=[]) is empty


# ---------------------------------------------------------------------------
# the contract the caller depends on
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_first_deny_wins_and_later_checks_never_run() -> None:
    calls: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    bundle = _gated([_tool("scan", calls)], [_deny("first"), _record(later)])

    with pytest.raises(PolicyDenied, match="first"):
        await bundle.tools[0].handler({})

    assert later == [], "a check after the deny still ran"
    assert calls == []


@pytest.mark.anyio
async def test_checks_see_the_mcp_qualified_name() -> None:
    """Parity with the SDK path — the whole reason `server` is a parameter."""
    seen: list[dict[str, Any]] = []
    bundle = _gated([_tool("ssh_exec", [])], [_record(seen)], agent_name="ssh", server="ssh")

    await bundle.tools[0].handler({"command": "id"})

    assert seen[0]["tool_name"] == "mcp__ssh__ssh_exec"
    assert seen[0]["tool_input"] == {"command": "id"}


@pytest.mark.anyio
async def test_updated_input_reaches_both_later_checks_and_the_handler() -> None:
    """An operator edit that the wrapper drops is worse than no gate at all."""
    calls: list[dict[str, Any]] = []
    seen: list[dict[str, Any]] = []
    bundle = _gated(
        [_tool("wipe", calls)],
        [_rewrite({"command": "rm -rf /tmp/scratch"}), _record(seen)],
    )

    await bundle.tools[0].handler({"command": "rm -rf /"})

    assert seen[0]["tool_input"] == {"command": "rm -rf /tmp/scratch"}
    assert calls == [{"command": "rm -rf /tmp/scratch"}]


@pytest.mark.anyio
async def test_each_invocation_gets_a_distinct_tool_use_id() -> None:
    seen: list[dict[str, Any]] = []
    bundle = _gated([_tool("scan", [])], [_record(seen)])

    await asyncio.gather(*(bundle.tools[0].handler({"i": i}) for i in range(5)))

    ids = {entry["tool_use_id"] for entry in seen}
    assert len(ids) == 5, "reused tool_use_id would collide in a replay cache"
    assert all(i.startswith("provider-") for i in ids)


def test_tool_identity_survives_the_wrap() -> None:
    calls: list[dict[str, Any]] = []
    original = _tool("scan", calls)
    bundle = _gated([original], [_allow()])

    assert bundle.tools[0].name == original.name
    assert bundle.tools[0].description == original.description
    assert dict(bundle.tools[0].input_schema) == dict(original.input_schema)
    assert bundle.tools[0].handler is not original.handler


# ---------------------------------------------------------------------------
# annotations
# ---------------------------------------------------------------------------


def test_every_tool_is_marked_gated() -> None:
    bundle = _gated([_tool("a", []), _tool("b", [])], [_allow()])
    assert all(t.annotations[POLICY_GATE_ANNOTATION] is True for t in bundle.tools)


def test_budget_is_published_only_when_non_zero() -> None:
    without = _gated([_tool("a", [])], [_allow()])
    assert POLICY_GATE_BUDGET_ANNOTATION not in without.tools[0].annotations

    with_budget = _gated([_tool("a", [])], [_allow()], gate_budget_sec=600)
    assert with_budget.tools[0].annotations[POLICY_GATE_BUDGET_ANNOTATION] == 600


def test_pre_existing_annotations_are_preserved() -> None:
    async def handler(_args):
        return {}

    tool = AgentTool("a", "", {"type": "object"}, handler, {"vendor/x": "keep"})
    bundle = _gated([tool], [_allow()])

    assert bundle.tools[0].annotations["vendor/x"] == "keep"
    assert bundle.tools[0].annotations[POLICY_GATE_ANNOTATION] is True


# ---------------------------------------------------------------------------
# the mixin must not grow a second copy
# ---------------------------------------------------------------------------


def test_the_mixin_delegates_rather_than_reimplementing() -> None:
    """Drift pin.

    `_gate_provider_bundle` decides WHICH checks apply; the wrapping itself must
    stay in one place. If the mixin ever grows its own loop over `bundle.tools`
    again, the two copies diverge exactly the way the polybrain mirror did.
    """
    from salient_core.daemon._runner_factory import _RunnerFactoryMixin

    src = inspect.getsource(_RunnerFactoryMixin._gate_provider_bundle)

    assert "gate_tool_bundle(" in src, "the mixin no longer delegates to the shared wrapper"
    assert "for tool in bundle.tools" not in src, (
        "the mixin is wrapping handlers itself again — that is a second copy of "
        "the gate, which is how the previous mirror drifted"
    )


def test_the_public_symbol_is_importable_from_the_runtime_module() -> None:
    """Tutor and any other embedder import it from here, not from the daemon."""
    import salient_core.runtime as runtime

    assert callable(runtime.gate_tool_bundle)
    assert callable(runtime.PolicyDenied)
    sig = inspect.signature(runtime.gate_tool_bundle)
    assert set(sig.parameters) >= {"bundle", "agent_name", "server", "checks"}
