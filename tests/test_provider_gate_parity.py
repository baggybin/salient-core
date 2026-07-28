"""Provider runtimes must inherit the PreToolUse gate stack.

`_build_options` — where the Claude-SDK path registers safeguards,
`approve_before` and the token-budget floor — runs ONLY under `runtime is None`.
Every provider runtime (codex, polybrain, and whatever is registered next)
therefore reached tool handlers with NO safeguard evaluation, NO operator
consent gate and NO budget floor. Scope was the lone survivor, and only because
it lives inside the built handler rather than in a hook.

`AgentRunnerFactory._gate_provider_bundle` closes that at the one seam every
provider bundle passes through. These pins hold three separate properties:

1. **The gate fires** — deny paths for each rung, in the right order.
2. **The gate is faithful** — the operator's edit reaches the handler, the
   safeguard hook sees the same qualified name the SDK path produces, and a
   benign call is untouched.
3. **A future provider inherits it** — enforced against the LIVE provider
   registry, and by driving a real denial through each provider's actual
   dispatch path rather than asserting a parameter was plumbed.

Property 3 is the one that matters most: "provider #3 was passed a hook" is the
wrong invariant, because receiving a hook does not mean invoking it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from salient_core.codex_mcp import _gate_budget, _tool_timeout
from salient_core.daemon import _runner_factory
from salient_core.daemon._runner_factory import (
    _APPROVAL_TIMEOUT_SEC,
    _RunnerFactoryMixin,
)
from salient_core.policy import scope
from salient_core.policy.registry import PolicyDataset
from salient_core.policy.safeguards import SafeguardConfig
from salient_core.providers import builtin_provider_registry
from salient_core.runtime import (
    POLICY_GATE_ANNOTATION as _GATE_ANNOTATION,
)
from salient_core.runtime import (
    POLICY_GATE_BUDGET_ANNOTATION,
    AgentTool,
    PolicyDenied,
    ToolBundle,
)
from salient_core.runtime import (
    POLICY_GATE_BUDGET_ANNOTATION as _GATE_BUDGET_ANNOTATION,
)

# The token-budget floor (T2.3) is a salient-core-private subsystem that has not
# been ported to the public snapshot — `_budget.py` and `_make_budget_gate_hook`
# simply are not there. Probe for it rather than forking this file, so the SAME
# suite runs in both repos and a future wholesale copy stays clean.
_HAS_BUDGET_GATE = hasattr(_RunnerFactoryMixin, "_make_budget_gate_hook")
_needs_budget_gate = pytest.mark.skipif(
    not _HAS_BUDGET_GATE, reason="token-budget floor not present in this build"
)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class _Runner:
    def __init__(self, dataset: PolicyDataset, cfg: dict[str, Any] | None = None) -> None:
        self.cfg: dict[str, Any] = cfg or {}
        self._policy_dataset = dataset
        self._safeguard_config = SafeguardConfig()
        self._enforce_builtin_policy = True
        self._budget_gate_armed = False
        self.total_safeguard_blocks = 0
        self.options = SimpleNamespace(tools=[])
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def _record_jsonl(self, event: str, payload: dict[str, Any]) -> None:
        json.dumps(payload, default=str)
        self.records.append((event, payload))


class _Daemon(_RunnerFactoryMixin):
    """Minimal daemon surface the gate touches."""

    def __init__(self, runner: _Runner, store: scope.ScopeStore) -> None:
        self.runners = {"agent": runner}
        self.scope = store
        self.profile: dict[str, Any] = {}
        self.engagement_path = None
        self.asked: list[tuple[str, str, str, list[str]]] = []
        self.answers: list[str] = []
        self.expired: list[tuple[int, str]] = []
        self.inbox = SimpleNamespace(
            expire=lambda qid, text: self.expired.append((qid, text)),
        )
        self._qid = 0

    def add_tool_approval_question(
        self, caller: str, tool_label: str, summary: str, categories: list[str]
    ) -> tuple[int, asyncio.Future]:
        self._qid += 1
        self.asked.append((caller, tool_label, summary, list(categories)))
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        if self.answers:
            future.set_result(self.answers.pop(0))
        return self._qid, future


def _dataset(
    targets: dict[str, scope.ExtractorSpec] | None = None,
    *,
    prohibited: dict[str, list[tuple[str, str]]] | None = None,
) -> PolicyDataset:
    return PolicyDataset(
        tool_targets=targets or {},
        prohibited_patterns=prohibited or {},
        loud_patterns={},
    )


def _tool(name: str, calls: list[dict[str, Any]]) -> AgentTool:
    async def handler(args):
        calls.append(dict(args))
        return {"ran": True}

    return AgentTool(name, "a tool", {"type": "object"}, handler)


def _gated(
    daemon: _Daemon,
    bundle: ToolBundle,
    *,
    cfg: dict[str, Any] | None = None,
) -> ToolBundle:
    agent_cfg: dict[str, Any] = {"name": "agent", **(cfg or {})}
    daemon.runners["agent"].cfg = agent_cfg
    return daemon._gate_provider_bundle(agent_cfg, bundle)


def _use_action_class(monkeypatch: pytest.MonkeyPatch, classes: set[str]) -> None:
    module = SimpleNamespace(classify_tool_action=lambda _type, _tool, _input: set(classes))
    monkeypatch.setattr(_runner_factory, "get_daemon_skin_module", lambda _name: module)


# ---------------------------------------------------------------------------
# 1. the gate is installed
# ---------------------------------------------------------------------------


def test_every_tool_in_a_provider_bundle_is_wrapped() -> None:
    calls: list[dict[str, Any]] = []
    original = _tool("scan", calls)
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "wrap"))

    bundle = _gated(daemon, ToolBundle((original, _tool("probe", calls))))

    assert len(bundle.tools) == 2
    for tool in bundle.tools:
        assert tool.annotations[_GATE_ANNOTATION] is True
    # The handler is genuinely replaced, not merely annotated.
    assert bundle.tools[0].handler is not original.handler
    # Identity/schema survive the wrap — the model must see the same tool.
    assert [t.name for t in bundle.tools] == ["scan", "probe"]
    assert bundle.tools[0].description == "a tool"


def test_empty_bundle_is_returned_untouched() -> None:
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "empty"))
    empty = ToolBundle()
    assert daemon._gate_provider_bundle({"name": "agent"}, empty) is empty


def test_gate_budget_annotation_only_for_agents_that_can_block_on_a_human() -> None:
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "budget-annot"))
    calls: list[dict[str, Any]] = []

    ungated = _gated(daemon, ToolBundle((_tool("scan", calls),)))
    assert _GATE_BUDGET_ANNOTATION not in ungated.tools[0].annotations

    gated = _gated(
        daemon,
        ToolBundle((_tool("scan", calls),)),
        cfg={"policy": {"approve_before": ["destructive"]}},
    )
    assert gated.tools[0].annotations[_GATE_BUDGET_ANNOTATION] == _APPROVAL_TIMEOUT_SEC


def test_both_build_provider_tool_bundle_paths_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bus-only agent (no `tool:` block) is gated too.

    `ask_agent` / `ask_agents` are bus tools; a delegation fan-out is precisely
    the call that must not slip past the prohibited-intent denylist, so the
    early return had to be wrapped as well as the main one.
    """
    calls: list[dict[str, Any]] = []
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "paths"))
    bus = ToolBundle((_tool("ask_agents", calls),))
    monkeypatch.setattr(_runner_factory, "make_bus_tool_bundle", lambda *_a, **_k: (bus, {}))

    # Path A: no `tool:` block → the early return.
    bus_only = daemon._build_provider_tool_bundle({"name": "agent"})
    assert bus_only.tools[0].annotations[_GATE_ANNOTATION] is True

    # Path B: a tool surface → the builder return.
    surface = ToolBundle((_tool("exploit", calls),))
    monkeypatch.setattr(
        _runner_factory, "get_tool_bundle_builder", lambda: lambda *_a, **_k: surface
    )
    with_surface = daemon._build_provider_tool_bundle(
        {"name": "agent", "tool": {"type": "msf", "config": {}}}
    )
    assert with_surface.tools[0].annotations[_GATE_ANNOTATION] is True


# ---------------------------------------------------------------------------
# 2. the gate denies — one pin per rung, plus ordering
# ---------------------------------------------------------------------------


@_needs_budget_gate
@pytest.mark.anyio
async def test_budget_parked_agent_is_denied_before_the_handler_runs() -> None:
    calls: list[dict[str, Any]] = []
    runner = _Runner(_dataset())
    runner._budget_gate_armed = True
    daemon = _Daemon(runner, scope.ScopeStore(None, "budget-deny"))
    bundle = _gated(daemon, ToolBundle((_tool("scan", calls),)))

    with pytest.raises(PolicyDenied) as caught:
        await bundle.tools[0].handler({"target": "10.0.0.1"})

    assert "budget" in str(caught.value).lower()
    assert calls == []


@pytest.mark.anyio
async def test_prohibited_intent_is_denied_on_the_provider_path() -> None:
    """The headline regression: safeguards never ran on a provider runtime."""
    calls: list[dict[str, Any]] = []
    dataset = _dataset(
        {"agent.scan": scope.ExtractorSpec(none=True)},
        prohibited={"agent.scan": [("blocked", "prohibited-marker")]},
    )
    daemon = _Daemon(_Runner(dataset), scope.ScopeStore(None, "safeguard-deny"))
    bundle = _gated(daemon, ToolBundle((_tool("scan", calls),)))

    with pytest.raises(PolicyDenied):
        await bundle.tools[0].handler({"note": "prohibited-marker"})
    assert calls == []

    # ...and the benign sibling call on the same tool still runs.
    assert await bundle.tools[0].handler({"note": "ordinary"}) == {"ran": True}
    assert calls == [{"note": "ordinary"}]


@pytest.mark.anyio
async def test_approve_before_denies_when_the_operator_says_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_action_class(monkeypatch, {"destructive"})
    calls: list[dict[str, Any]] = []
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "approve-no"))
    daemon.answers = ["no too risky"]
    bundle = _gated(
        daemon,
        ToolBundle((_tool("wipe", calls),)),
        cfg={"policy": {"approve_before": ["destructive"]}},
    )

    with pytest.raises(PolicyDenied) as caught:
        await bundle.tools[0].handler({"command": "rm -rf /"})

    assert "denied" in str(caught.value).lower()
    assert calls == []
    assert daemon.asked and daemon.asked[0][1] == "wipe"


@pytest.mark.anyio
async def test_approve_before_allows_when_the_operator_says_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_action_class(monkeypatch, {"restricted"})
    calls: list[dict[str, Any]] = []
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "approve-yes"))
    daemon.answers = ["yes"]
    bundle = _gated(
        daemon,
        ToolBundle((_tool("act", calls),)),
        cfg={"policy": {"approve_before": ["restricted"]}},
    )

    assert await bundle.tools[0].handler({"target": "10.0.0.1"}) == {"ran": True}
    assert calls == [{"target": "10.0.0.1"}]


@pytest.mark.anyio
async def test_unlisted_action_class_is_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The benign path: a policy that doesn't cover this call must not prompt."""
    _use_action_class(monkeypatch, {"recon"})
    calls: list[dict[str, Any]] = []
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "ungated-class"))
    bundle = _gated(
        daemon,
        ToolBundle((_tool("scan", calls),)),
        cfg={"policy": {"approve_before": ["destructive"]}},
    )

    assert await bundle.tools[0].handler({"target": "10.0.0.1"}) == {"ran": True}
    assert daemon.asked == []


@_needs_budget_gate
@pytest.mark.anyio
async def test_budget_deny_precedes_the_interactive_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering pin: a budget-parked agent must never prompt the operator.

    The budget gate is the cheapest deny and sits FIRST in the pipeline. If a
    refactor reorders the checks, a parked agent would file an approval question
    for a call that can never run.
    """
    _use_action_class(monkeypatch, {"destructive"})
    calls: list[dict[str, Any]] = []
    runner = _Runner(_dataset())
    runner._budget_gate_armed = True
    daemon = _Daemon(runner, scope.ScopeStore(None, "order"))
    bundle = _gated(
        daemon,
        ToolBundle((_tool("wipe", calls),)),
        cfg={"policy": {"approve_before": ["destructive"]}},
    )

    with pytest.raises(PolicyDenied) as caught:
        await bundle.tools[0].handler({"command": "rm -rf /"})

    assert "budget" in str(caught.value).lower()
    assert daemon.asked == []  # never reached the human
    assert calls == []


# ---------------------------------------------------------------------------
# 3. the gate is faithful
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_operator_edit_reaches_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """`updatedInput` must be threaded into the call.

    `approve_before` supports an `edit: <command>` verdict. If the wrapper
    discards `updatedInput`, the ORIGINAL command runs while the operator is
    told their edit was applied — a control-honesty failure introduced BY the
    gate, which is worse than the hole it closes.
    """
    _use_action_class(monkeypatch, {"destructive"})
    calls: list[dict[str, Any]] = []
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "edit"))
    daemon.answers = ["edit: rm -rf /tmp/scratch"]
    bundle = _gated(
        daemon,
        ToolBundle((_tool("wipe", calls),)),
        cfg={"policy": {"approve_before": ["destructive"]}},
    )

    await bundle.tools[0].handler({"command": "rm -rf /"})

    assert calls[0]["command"] == "rm -rf /tmp/scratch"
    # destructive + a command field ⇒ the confirm flag rides along too.
    assert calls[0]["confirm_destructive"] is True


@pytest.mark.anyio
async def test_confirm_destructive_is_injected_on_plain_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_action_class(monkeypatch, {"destructive"})
    calls: list[dict[str, Any]] = []
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "confirm"))
    daemon.answers = ["yes"]
    bundle = _gated(
        daemon,
        ToolBundle((_tool("wipe", calls),)),
        cfg={"policy": {"approve_before": ["destructive"]}},
    )

    await bundle.tools[0].handler({"command": "shred disk"})

    assert calls[0]["confirm_destructive"] is True
    assert calls[0]["command"] == "shred disk"


@pytest.mark.anyio
async def test_safeguard_hook_sees_the_mcp_qualified_name() -> None:
    """Classification parity with the SDK path.

    `_make_safeguard_hook` BRANCHES on the `mcp__` prefix: MCP-form names get
    safeguards-then-allow (scope already lives in the handler), bare names take
    the built-in policy path with a SECOND scope evaluation. Feeding bare wire
    names would silently take the wrong branch — a gate that looks armed and
    classifies wrong. The dataset key below is the MCP-derived qualified name,
    so a deny here proves the synthesized form is what the hook received.
    """
    calls: list[dict[str, Any]] = []
    dataset = _dataset(
        {"agent.scan": scope.ExtractorSpec(none=True)},
        prohibited={"agent.scan": [("blocked", "prohibited-marker")]},
    )
    daemon = _Daemon(_Runner(dataset), scope.ScopeStore(None, "qualified"))
    bundle = _gated(daemon, ToolBundle((_tool("scan", calls),)))

    with pytest.raises(PolicyDenied):
        await bundle.tools[0].handler({"note": "prohibited-marker"})

    # A bare-name lookup would have keyed on "builtin.scan" and found nothing.
    assert calls == []


@pytest.mark.anyio
async def test_arguments_are_passed_through_unmodified_when_nothing_gates() -> None:
    calls: list[dict[str, Any]] = []
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "passthrough"))
    bundle = _gated(daemon, ToolBundle((_tool("scan", calls),)))

    payload = {"target": "10.0.0.1", "flags": ["-sV"], "depth": 3}
    assert await bundle.tools[0].handler(payload) == {"ran": True}
    assert calls == [payload]


@pytest.mark.anyio
async def test_concurrent_calls_do_not_collide_in_the_replay_cache() -> None:
    """Each invocation mints a fresh tool_use_id.

    `HookReplayCache` rejects a reused id whose arguments differ. There is no
    SDK-issued id on this path, so a shared constant would make two concurrent
    calls to the same tool deny each other.
    """
    calls: list[dict[str, Any]] = []
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "concurrent"))
    bundle = _gated(daemon, ToolBundle((_tool("scan", calls),)))
    handler = bundle.tools[0].handler

    results = await asyncio.gather(
        handler({"target": "10.0.0.1"}),
        handler({"target": "10.0.0.2"}),
        handler({"target": "10.0.0.3"}),
    )

    assert results == [{"ran": True}] * 3
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# 4. a future provider inherits the gate
# ---------------------------------------------------------------------------

# Providers for which THIS FILE drives a real denial through the provider's own
# dispatch path. A newly registered provider must be added here together with a
# dispatch pin — see test_every_registered_provider_has_a_dispatch_pin.
_DISPATCH_PINNED: frozenset[str] = frozenset({"claude", "codex", "polybrain"})


def test_every_registered_provider_has_a_dispatch_pin() -> None:
    """The enforceable version of "future providers inherit the gate".

    Asserting that a provider was PASSED a hook is the wrong invariant —
    receiving one does not mean invoking it. This pin instead forces every
    registered provider to have a real dispatch test in this file, so a provider
    that dispatches around `AgentTool.handler` cannot land unnoticed.
    """
    registered = {provider.name for provider in builtin_provider_registry().providers()}
    missing = registered - _DISPATCH_PINNED
    assert not missing, (
        f"provider(s) {sorted(missing)} are registered but have no dispatch pin in "
        f"test_provider_gate_parity.py. Add one that drives a PolicyDenied through "
        f"the provider's own tool-dispatch path and assert the handler never ran."
    )


def test_policy_denied_is_an_oserror_subclass() -> None:
    """Load-bearing: the codex gateway's existing catch clause covers OSError.

    If `PolicyDenied` stopped being an `OSError`, a denial would escape
    `_dispatch`'s handler entirely instead of rendering as an error tool result.
    """
    assert issubclass(PolicyDenied, OSError)
    assert issubclass(PolicyDenied, PermissionError)


@pytest.mark.anyio
async def test_codex_gateway_renders_a_denial_as_an_error_result() -> None:
    """codex dispatch pin: a gated refusal reaches the model as isError."""
    from salient_core.codex_mcp import CodexMcpGateway

    ran = threading.Event()

    async def handler(_arguments):
        ran.set()
        return {"ok": True}

    async def denied(_arguments):
        raise PolicyDenied("prohibited by safeguards", tool="t", agent="agent")

    gateway = CodexMcpGateway()
    gateway.start()
    try:
        credential = gateway.issue(
            "owner",
            ToolBundle(
                (
                    AgentTool("t", "", {"type": "object"}, denied),
                    AgentTool("ok", "", {"type": "object"}, handler),
                )
            ),
        )
        catalog = gateway._catalog(credential.token)
        assert catalog is not None

        # _dispatch blocks polling its future, so it must run OFF the loop that
        # has to execute the handler coroutine.
        status, result = await asyncio.to_thread(
            gateway._dispatch,
            catalog,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "t", "arguments": {}},
            },
        )

        assert status == 200
        assert result["result"]["isError"] is True
        assert "prohibited by safeguards" in result["result"]["content"][0]["text"]
        assert not ran.is_set()
    finally:
        gateway.close()


@pytest.mark.anyio
async def test_polybrain_backend_surfaces_a_denial_as_an_error() -> None:
    """polybrain dispatch pin: same property, through its own tool loop."""
    from salient_core.polybrain.backend import PolybrainBackend, PolybrainBackendConfig
    from salient_core.polybrain.types import BrainToolCall

    async def denied(_arguments):
        raise PolicyDenied("token budget exhausted", tool="t", agent="agent")

    backend = PolybrainBackend(
        PolybrainBackendConfig(brain="minimax", model="MiniMax-M3"),
        brain=SimpleNamespace(),
        tool_bundle=ToolBundle((AgentTool("t", "", {"type": "object"}, denied),)),
    )

    text, is_error = await backend._execute_tool(BrainToolCall(id="1", name="t", arguments={}))

    assert is_error is True
    assert "token budget exhausted" in text


def test_claude_provider_refuses_a_provider_bundle_outright() -> None:
    """claude dispatch pin — satisfied by refusal rather than by wrapping.

    `claude` is a registered provider and is reachable as an explicit
    `runtime.provider`, but it wires tools through pre-built options rather than
    the bundle seam. Handed a bundle it fails LOUD instead of quietly running a
    tool-less backend, so there is no ungated dispatch path through it — which
    is why it needs no wrap.

    If that refusal ever softens into a silent drop, this pin fails, and the
    right response is to give the provider a real gated dispatch path rather
    than to delete the assertion.
    """
    from salient_core.daemon._backend import ClaudeProvider, ClaudeProviderConfigError

    calls: list[dict[str, Any]] = []
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "claude-bundle"))
    bundle = _gated(daemon, ToolBundle((_tool("scan", calls),)))

    with pytest.raises(ClaudeProviderConfigError):
        ClaudeProvider().create_backend({}, tool_bundle=bundle)

    # The empty-bundle case (the default no-`runtime:` path) stays accepted.
    assert ClaudeProvider().create_backend({}) is not None


# ---------------------------------------------------------------------------
# 5. the operator's answer window survives the provider's own deadline
# ---------------------------------------------------------------------------


def test_gate_budget_extends_the_gateway_ceiling() -> None:
    """Without this the 120s default cancels the call mid-question.

    The approval wait happens INSIDE the handler on the provider path, so it is
    charged against the tool's ceiling. A 120s bound would cut the operator's
    10-minute window to two minutes, report a bare timeout rather than an honest
    refusal, and orphan the question in the inbox.
    """
    base = _tool_timeout("msf.exploit")
    gated = _tool_timeout("msf.exploit", None, None, {POLICY_GATE_BUDGET_ANNOTATION: 600})
    assert gated == base + 600
    assert gated > _APPROVAL_TIMEOUT_SEC


def test_gate_budget_is_clamped_and_rejects_junk() -> None:
    assert _gate_budget(None) == 0
    assert _gate_budget({}) == 0
    assert _gate_budget({POLICY_GATE_BUDGET_ANNOTATION: "600"}) == 0
    assert _gate_budget({POLICY_GATE_BUDGET_ANNOTATION: True}) == 0  # bool is not a duration
    assert _gate_budget({POLICY_GATE_BUDGET_ANNOTATION: 0}) == 0
    assert _gate_budget({POLICY_GATE_BUDGET_ANNOTATION: -5}) == 0
    assert _gate_budget({POLICY_GATE_BUDGET_ANNOTATION: 10**9}) == 900  # clamped


def test_blocking_delegation_tools_keep_their_own_ceiling() -> None:
    """`ask_*` already sits above the bus cap; the budget must not inflate it."""
    plain = _tool_timeout("ask_agent")
    with_budget = _tool_timeout("ask_agent", None, None, {POLICY_GATE_BUDGET_ANNOTATION: 600})
    assert plain == with_budget


def test_declared_timeout_still_clamped_before_the_budget_is_added() -> None:
    """The clamp bounds MODEL-controlled input; the budget is the daemon's own."""
    huge = _tool_timeout("keyspace", {"timeout_s": 10**9}, None, None)
    with_budget = _tool_timeout(
        "keyspace", {"timeout_s": 10**9}, None, {POLICY_GATE_BUDGET_ANNOTATION: 600}
    )
    assert with_budget == huge + 600


# ---------------------------------------------------------------------------
# 6. the drifted mirror is gone
# ---------------------------------------------------------------------------


def test_polybrain_bespoke_safeguard_mirror_is_removed() -> None:
    """`_make_polybrain_safeguard_hook` drifted from what it mirrored.

    It ran a bare `check_intent` instead of the full `evaluate_safeguards` (no
    posture/loud patterns, no strike counter, no audit mirror), carried a
    simplified `approve_before` with no edit verdicts, and omitted the token
    budget entirely. It is replaced by the shared pipeline; if it comes back,
    polybrain gets double-gated and the operator is prompted twice.
    """
    assert not hasattr(_RunnerFactoryMixin, "_make_polybrain_safeguard_hook")


def test_provider_gate_checks_reuse_the_real_hook_factories() -> None:
    """Order and membership, asserted against the real pipeline builder."""
    daemon = _Daemon(_Runner(_dataset()), scope.ScopeStore(None, "pipeline"))

    expected = 2 if _HAS_BUDGET_GATE else 1  # (budget gate) + safeguards
    plain = daemon._provider_gate_checks({"name": "agent"})
    assert len(plain) == expected

    with_policy = daemon._provider_gate_checks(
        {"name": "agent", "policy": {"approve_before": ["destructive"]}}
    )
    assert len(with_policy) == expected + 1  # + approve_before, last
