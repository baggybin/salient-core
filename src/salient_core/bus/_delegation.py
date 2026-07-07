"""Bus delegation tools — ask_agent, ask_partner, ask_agents, ask_operator.

The four "talk to someone" tools. Extracted from salient/bus.py
during the package split; @tool closure shape preserved verbatim so
daemon/owner capture is identical.

ask_agent is the load-bearing one at ~440 lines — it carries delegation
routing, alias resolution, cycle detection, operator approval gating,
and the full ledger-of-pending-calls integration. Behavior is
unchanged by this extraction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, Field

from ._common import *  # noqa: F401,F403
from ._common import bus_tool
from ._flags import BusFlags

# ── Delegation-observer seam ─────────────────────────────────────────
# A downstream skin may watch a delegated child's event stream, learn the
# child job id, and optionally transform the reply — WITHOUT the kernel knowing
# what it does. ``ask_agent`` builds one observer per dispatch from the
# registered factory and calls three opaque methods on it: ``on_submit`` (at
# submit, so an observer can learn the child job id — e.g. to cancel it),
# ``record`` (per child event, from the echo loop, BEFORE the display
# recursion-guard so the observer sees the full subtree), and ``finalize``
# (the reply, returning a possibly-transformed reply). The default is a no-op,
# so the kernel is unchanged with no factory registered. Same call-time
# injection idiom as set_bus_builder / set_kg_assert_hook.
#
# NOTE: ``record`` MAY be called more than once for the same event (the live
# echo pump plus a post-completion drain), so an observer's per-event handling
# must be idempotent.


class _NullDelegationObserver:
    """Default no-op observer (kernel behaviour: nothing is observed)."""

    __slots__ = ()

    def record(self, evt: dict) -> None: ...
    def on_submit(self, child_job: Any) -> None: ...
    def finalize(self, reply: str) -> str:
        return reply


_NULL_DELEGATION_OBSERVER = _NullDelegationObserver()
_delegation_observer_factory: Callable[..., Any] | None = None


def set_delegation_observer(factory: Callable[..., Any]) -> None:
    """Register a per-dispatch delegation-observer factory. Called once at
    startup by a skin. ``factory(daemon, owner, runner)`` returns an observer
    exposing ``record(evt)`` / ``on_submit(child_job)`` / ``finalize(reply)`` —
    or ``None`` for no observer on that dispatch. Default: no observer."""
    global _delegation_observer_factory
    _delegation_observer_factory = factory


def _make_delegation_observer(daemon: Any, owner: str, runner: Any) -> Any:
    """Build the observer for one dispatch (never raises; never returns None)."""
    factory = _delegation_observer_factory
    if factory is None:
        return _NULL_DELEGATION_OBSERVER
    try:
        obs = factory(daemon, owner, runner)
    except Exception:  # noqa: BLE001 — a broken skin factory must not break dispatch
        return _NULL_DELEGATION_OBSERVER
    return obs if obs is not None else _NULL_DELEGATION_OBSERVER


# ── Agent-disablement seam ───────────────────────────────────────────
# WHETHER an agent is operator-disabled is engagement/skin policy, read off the
# skin's profile shape. A downstream registers its ``(profile, name) -> bool``
# check via ``set_agent_disabled_checker``; ask_agent refuses a dispatch to a
# disabled agent before any other gate. Default: nothing is disabled (a kernel
# with no engagement policy imposes no disablement), so the check is a no-op
# until a skin registers.
_agent_disabled_checker: Callable[[Any, str], bool] | None = None


def set_agent_disabled_checker(check: Callable[[Any, str], bool]) -> None:
    """Register the engagement agent-disablement check: ``(profile, name) ->
    bool``. Called once at startup by a skin. Default: nothing disabled."""
    global _agent_disabled_checker
    _agent_disabled_checker = check


# Wire schema for ask_agent. Optionals carry NEUTRAL defaults (0/""/False), so
# per the migration pattern they need no field description or validator — the
# rich per-param docs live in the tool description below. bus_tool strips the
# `default` keyword, so each optional advertises the bare `{"type": ...}` the
# pre-migration inline schema did; the required list stays [name, prompt] (the
# small-model "max_turns is a required property" bug was already fixed there).
class _AskAgentArgs(BaseModel):
    name: str
    prompt: str
    max_turns: int = 0
    deliverable: str = ""
    prefer_primary: bool = False


class _AskPartnerArgs(BaseModel):
    prompt: str
    max_turns: int = 0
    deliverable: str = ""


class _AskAgentsArgs(BaseModel):
    # Each child is exactly an ask_agent envelope — reuse the model so the nested
    # items schema (required [name, prompt]; optional max_turns/deliverable/
    # prefer_primary) stays in lockstep. _clean_tool_schema inlines the $ref.
    # min/max_length reproduce the wire minItems:1/maxItems:20 the handler used
    # to enforce by hand (an empty / >20 list is now a validation error).
    children: list[_AskAgentArgs] = Field(min_length=1, max_length=20)
    aggregate: Literal["all", "any", "race"] = Field(
        "all",
        description="'all' gather every child; 'any' return on first success; "
        "'race' return the first to finish. Defaults to 'all'.",
    )
    # Dynamic default: omitted (or explicit null) ⇒ auto = min(len(children), 10).
    # None is the only correct sentinel (not falsy-0 — explicit null must also
    # mean auto); an explicit value is bounded 1..20 (now enforced by ge=/le=).
    concurrency: int | None = Field(
        None,
        ge=1,
        le=20,
        description="max children in flight; defaults to auto = min(children, 10).",
    )
    deliverable: str = ""


class _AskOperatorArgs(BaseModel):
    question: str


if TYPE_CHECKING:
    from ..protocols import DaemonServices


_log = logging.getLogger("salient.bus.delegation")


def _pick_substitute(all_cfgs: dict[str, Any], runners: dict[str, Any], name: str) -> str | None:
    """The lowest-named RUNNING substitute for ``name``, or None.

    Name-stable so substitute routing doesn't depend on agents.yaml
    insertion order when more than one substitute for the same primary
    is up at once (rare, but the order-dependence was a real fragility).
    """
    for sub_name in sorted(all_cfgs or {}):
        if (all_cfgs.get(sub_name) or {}).get("substitute_for") != name:
            continue
        r = runners.get(sub_name)
        if r is not None and r.status != "stopped":
            return sub_name
    return None


async def _record_approval_bypass(
    daemon: Any,
    caller: str,
    target: str,
    gate: str,
    prompt: str,
    bus_trusted: Any,
) -> None:
    """Audit a `bus_trusted` caller skipping an operator approval gate (D-1).

    A trusted caller bypasses the agent-start / delegation gate with no
    operator answer, so historically there was no record at all. Write two
    layers: (a) an in-the-moment provenance event on the caller's transcript
    (which also lands in the durable `events` table) plus a `_log.info` line,
    and (b) a row in the dedicated, never-pruned `approval_bypass` table — the
    durable system-of-record queryable via the `bypasses_list` RPC. Both
    writes are best-effort: an audit failure must never break the delegation.
    """
    snippet = (prompt or "")[:500]
    scope = "all" if bus_trusted is True else "list"
    with suppress(Exception):
        caller_runner = daemon.runners.get(caller)
        if caller_runner is not None:
            await caller_runner._log_provenance(
                "approval_bypass",
                f"bypassed operator {gate} gate → {target} (trust:{scope}): {snippet}",
                source=caller,
                recipient=target,
                extras={"gate": gate, "trust_scope": scope},
            )
    _log.info(
        "ask_agent bypass: caller=%s target=%s gate=%s scope=%s",
        caller,
        target,
        gate,
        scope,
    )
    with suppress(Exception):
        daemon.context.record_bypass(caller, target, gate, snippet, scope)


async def _echo_child_stream(
    child_q: asyncio.Queue,
    caller_runner: Any,
    *,
    child: str,
    child_job_id: int,
    observer: Any = _NULL_DELEGATION_OBSERVER,
) -> None:
    """Mirror a delegated child's live events onto the CALLER's pane while we
    await its reply, so the operator sees nested child activity in line with
    the caller's own stream. The MODEL never sees this — the SDK has no
    streaming tool-result type; this is operator visibility only (and matches
    the always-on bus-substitute / bus-redact precedent).

    Two filters keep it correct:
      • job_id — a runner serially interleaves jobs from many callers, and our
        dispatch may sit behind others in the queue. Echo only events tagged
        with OUR job's id (requires the job_id stamp added in _publish), so a
        concurrently-running different job can't leak onto the caller's pane.
      • recursion guard — drop events that are themselves delegated echoes
        (``meta.delegated_from`` set). In a chain A→B→C this keeps each pane
        showing its DIRECT child only and stops ``delegated:delegated:…`` kinds
        from growing unbounded.

    Runs as a task; cancelled and unsubscribed by ask_agent's finally. It must
    NOT unsubscribe itself (it can be cancelled mid-`get`) — the queue
    lifecycle is owned by the caller."""
    while True:
        evt = await child_q.get()
        if evt.get("job_id") != child_job_id:
            continue
        # Skin observer sees the full subtree — including delegated echoes —
        # BEFORE the display recursion-guard drops them (a skin unwraps echoes
        # to see a grandchild's events; the operator pane shows the direct
        # child only). Best-effort: a raising observer can't break the echo.
        with suppress(Exception):
            observer.record(evt)
        if (evt.get("meta") or {}).get("delegated_from"):
            continue
        with suppress(Exception):
            caller_runner._publish(
                f"delegated:{evt.get('kind', '')}",
                evt.get("text", ""),
                meta={
                    "delegated_from": child,
                    "child_job_id": child_job_id,
                    "child_kind": evt.get("kind", ""),
                    # Preserve the ORIGINAL descendant event so an observer ONE
                    # HOP UP can still read its structured meta (tool_call etc.);
                    # the flat kind/text projection above loses it. Renderer-safe
                    # (the pane reads only delegated_from/child_job_id/child_kind;
                    # the recursion guard keeps this single-level so there is no
                    # delegated_event.delegated_event nesting).
                    "delegated_event": evt,
                },
            )


def make_delegation_tools(daemon: DaemonServices, owner: str) -> list:
    """Returns [ask_agent, ask_agents, ask_partner, ask_operator] in the
    order specified by _BUS_TOOL_NAMES."""

    @bus_tool(
        "ask_agent",
        "Send a prompt to ANOTHER running agent and wait for its reply. "
        "Use this to delegate sub-tasks. Returns the agent's final text reply. "
        "Use list_agents to see who you can ask.\n"
        "\n"
        "Delegation envelope params (both are Optional — see below for "
        "exact semantics):\n"
        "  name         — REQUIRED. The agent to delegate to.\n"
        "  prompt       — REQUIRED. The task body. Per Recipe & writeup "
        "discipline: describe the SHAPE of the task, don't paste a "
        "fully-substituted multi-step runbook.\n"
        "  max_turns    — SOFT HINT on the child's turn count. "
        "Optional (default: child's configured cap, ~30). The daemon "
        "does NOT enforce this — ClaudeAgentOptions.max_turns is baked "
        "at runner creation. The child sees the budget framed as a "
        "ceiling in its receiver envelope and the agent protocol "
        "instructs receivers to honor it, but the wire itself is "
        "advisory. Effectively: a strongly-honored hint. Use small "
        "numbers (3-8) for focused sub-tasks; the child self-"
        "terminates instead of drifting. Read the child's reply for "
        "what it actually used (not what you suggested).\n"
        "  deliverable  — one-line acceptance criterion the child must "
        "return. Optional (default: no constraint, child decides). "
        "Example: 'list of matching record ids as a comma-separated string' "
        "or 'one-paragraph summary plus the source id if found'. Setting "
        "this anchors the child on a concrete output shape and shortens "
        "their turns dramatically.\n"
        "  prefer_primary — Optional boolean (default: false). When "
        "TRUE, SKIP substitute routing so the call lands on the literal "
        "agent named. Use when your directive explicitly names the "
        "primary OVER the substitute — e.g. the operator/manager said "
        "'use module-runner (not deepseek_alt)'. The substitute-routing "
        "default is a cost-tier optimization; this flag is your wire-"
        "level opt-out for one call. Stopping the substitute via "
        "salientctl is the other valid path.",
        _AskAgentArgs,
        routed=True,
    )
    async def ask_agent(args: dict[str, Any], flags: BusFlags) -> dict[str, Any]:
        from ..alias import to_real as _alias_to_real

        # `args` is already validated + coerced to the declared fields by
        # @bus_tool, and `flags` is the typed routing channel it passes in:
        # BusFlags() on the wire path, the producer's typed flags in-process
        # (.trusted). bus_tool owns ingress — it drops any `_`-key on the wire
        # and raises on a stray one when trusted — so no adapter is needed here.

        name = (args.get("name") or "").strip()
        name = _alias_to_real(name)  # alias → real for runner lookup
        # Substitute routing — cost-tier default. When the caller names
        # a role (e.g. `wifi`) AND a configured agent with
        # `substitute_for: <name>` is running, prefer the substitute
        # over the primary. This makes the cheaper-tier shadow agents
        # (DeepSeek today) the default whenever they're up: start
        # `deepseek_wifi` and every existing `ask_agent('wifi', …)`
        # routes there. To force the Anthropic primary, stop the
        # substitute. Naming a substitute directly
        # (`ask_agent('deepseek_wifi', …)`) is treated as an explicit
        # intent and never redirected.
        # `_skip_substitute_routing` is an INTERNAL flag set by
        # ask_partner (the shadow→primary consultation path) so a
        # shadow's call to its primary actually reaches the primary,
        # not back to itself via substitute redirection. Not exposed
        # in the public schema; only internal callers set it.
        #
        # `prefer_primary` is the PUBLIC caller-facing escape hatch
        # (added 2026-05-16 after a real incident — manager said
        # "use module-runner (not deepseek_alt)" but routing
        # redirected anyway because the substitute was still up).
        # When set, the substitute loop is skipped entirely so the
        # call lands on the literal primary.
        prefer_primary = bool(args.get("prefer_primary"))
        substituted_from: str | None = None
        if not prefer_primary and not flags.skip_substitute_routing:
            _sub = _pick_substitute(daemon.all_cfgs, daemon.runners, name)
            if _sub is not None:
                substituted_from = name
                name = _sub
        # Pre-dispatch observability: when routing actually rewrites
        # the target, publish a bus-substitute event on the CALLER's
        # stream so the operator sees the rewrite the moment it
        # happens — not buried in the suffix banner on the reply.
        # Same shape as bus-redact below. Silent on no-op (no
        # substitute running, or prefer_primary set).
        if substituted_from is not None:
            caller_runner = daemon.runners.get(owner)
            if caller_runner is not None:
                with suppress(Exception):
                    caller_runner._publish(
                        "bus-substitute",
                        f"ask_agent({substituted_from!r}, …) routed to "
                        f"{name!r} — substitute is running. Pass "
                        f"prefer_primary=true to force {substituted_from!r}.",
                    )
        prompt = args.get("prompt") or ""
        # Soft envelope. Both optional; non-positive max_turns / empty
        # deliverable mean "no hint". We don't enforce max_turns hard —
        # ClaudeAgentOptions.max_turns is baked at runner creation — but
        # the child sees the budget in its prompt and tends to honor it.
        try:
            max_turns_hint = int(args.get("max_turns") or 0)
        except (TypeError, ValueError):
            max_turns_hint = 0
        deliverable = (args.get("deliverable") or "").strip()
        if not name or not prompt:
            return _text("error: name and prompt are required", error=True)
        if name == owner:
            return _text("error: cannot ask yourself", error=True)
        # Engagement-level disablement: refuse before any other gate fires.
        # Even bus_trusted callers respect this — operator policy beats
        # caller trust. WHICH agents are disabled is skin policy (registered
        # via set_agent_disabled_checker); the kernel default disables none.
        if _agent_disabled_checker is not None and _agent_disabled_checker(daemon.profile, name):
            return _text(
                f"error: agent {name!r} is DISABLED in the engagement "
                f"profile (operator-blocked). Tell the operator the work "
                f"needs this agent enabled if you genuinely need it.",
                error=True,
            )

        # Pending-operator-question conflict check. If a child agent has
        # filed an operator question naming a target, dispatching another
        # specialist against that same target while the operator hasn't
        # answered is a coordination bug — the parallel action undermines
        # the operator's decision in flight. bus_trusted callers (manager,
        # red_lead, blue_lead) ALSO respect this gate; bus_trusted bypasses
        # per-call approval, not coordination correctness.
        prompt_targets = _extract_targets_from_text(prompt)
        conflict_q = _conflicting_pending_question(daemon, prompt_targets)
        if conflict_q is not None:
            overlap = sorted(_extract_targets_from_text(conflict_q.text) & prompt_targets)
            target_list = ", ".join(repr(t) for t in overlap)
            return _text(
                f"error: pending operator question Q{conflict_q.id} (from "
                f"{conflict_q.agent!r}) is asking the operator about "
                f"{target_list}. Dispatching another specialist against "
                f"the same target undermines the operator decision in "
                f"flight. WAIT for the operator's answer to Q{conflict_q.id} "
                f"before re-dispatching, or address that question first.",
                error=True,
            )

        # Cross-team firewall (wire-level). Specialists on team 'red'
        # and team 'blue' cannot call each other directly; cross-team
        # coordination routes through `manager`. Fires even for
        # bus_trusted callers — same shape as the engagement-disabled
        # gate above; trust bypasses approval, not correctness. Leads
        # (team:lead) share a team across red_lead/blue_lead so they
        # aren't field-gateable here; their cross-team prose is pinned
        # by the lead-template tests instead.
        caller_team = ((daemon.all_cfgs.get(owner) or {}).get("team") or "").lower()
        target_team = ((daemon.all_cfgs.get(name) or {}).get("team") or "").lower()
        if (
            caller_team in ("red", "blue")
            and target_team in ("red", "blue")
            and caller_team != target_team
        ):
            return _text(
                f"error: cross-team delegation blocked. {owner!r} "
                f"(team {caller_team!r}) cannot ask_agent {name!r} "
                f"(team {target_team!r}) directly — cross-team "
                f"coordination routes through `manager`. Send your "
                f"request to manager with a one-line note on what "
                f"you need from the other side; manager will "
                f"dispatch.",
                error=True,
            )

        runner = daemon.runners.get(name)
        loop = asyncio.get_running_loop()
        # `bus_trusted` callers (sherlock, future orchestrators) skip both
        # the operator-approval delegation gate AND the agent-start question.
        # The operator has consciously delegated authority to this agent.
        # The value is a bool (trust ALL targets) or a list of agent names
        # (trust only those) — resolved per-target by `_trust_covers`, so
        # an out-of-list target falls back to the normal operator gate.
        # Every actual bypass is audited via `_record_approval_bypass` (D-1).
        caller_cfg = daemon.all_cfgs.get(owner) or {}
        bus_trusted = caller_cfg.get("bus_trusted")

        # Cycle detection (added 2026-05-18 after the 3-way deadlock that
        # sat silent for 7 minutes: agent_a → agent_b →
        # agent_c → agent_a). Walk the in-flight delegation
        # graph forward from the proposed target; if it reaches the caller,
        # this call would close a cycle. Refuse with the explicit path so
        # the caller (and operator, via stream) sees exactly which agents
        # are tangled. Cheap — O(N+E) over current _bus_calls, which is
        # bounded by concurrent delegations (~tens, not thousands).
        from ..coord.delegation_graph import find_cycle, format_cycle

        try:
            existing_calls = list((daemon._bus_calls or {}).values())
        except Exception:
            existing_calls = []
        cycle = find_cycle(existing_calls, owner, name)
        if cycle is not None:
            _log.info(
                "ask_agent refused cycle: %s (caller=%s target=%s)",
                format_cycle(cycle),
                owner,
                name,
            )
            return _text(
                f"error: cycle detected in delegation graph. Adding "
                f"this call would close the loop: {format_cycle(cycle)}. "
                f"Refusing to register — the caller would block forever "
                f"waiting on a chain that depends on the caller itself. "
                f"If you genuinely need {name!r} to do work, file an "
                f"ask_operator describing what you actually need and "
                f"the operator will break the cycle.",
                error=True,
            )

        # Runaway-delegation backstop: refuse calls that would nest too deep
        # or saturate the in-flight registry (depth/count caps). Checked here,
        # after the cycle gate and before register, so the refusal surfaces to
        # the caller the same way a cycle refusal does.
        _admit_refusal = daemon.bus_call_admission_check(owner)
        if _admit_refusal is not None:
            _log.info(
                "ask_agent refused (admission): caller=%s target=%s — %s",
                owner,
                name,
                _admit_refusal,
            )
            return _text(_admit_refusal, error=True)

        # Register this call in the daemon's bus registry so the operator can
        # `bus pending` to see what's blocked and `bus cancel` to unstick a
        # leaked future without nuking the asking agent. The call's tracked
        # future is updated as we transition through phases (agent-start gate
        # / delegation gate / awaiting reply); cancel sets exception on
        # whichever future is current.
        # `_parent_call_id` is an INTERNAL flag set by ask_agents (the swarm
        # fan-out path) so this call is registered as a child of the swarm
        # parent BusCall. Not in the public schema; ask_agents passes it
        # when invoking ask_agent under the hood. Same pattern as
        # `skip_substitute_routing` (set by ask_partner). BusFlags already
        # validated it to int | None, so no isinstance re-check.
        _parent_call_id = flags.parent_call_id
        placeholder: asyncio.Future = loop.create_future()
        call_id = daemon.bus_call_register(
            owner,
            name,
            prompt,
            placeholder,
            parent_call_id=_parent_call_id,
        )
        try:
            # Operator-visible delegated-stream echo (set up in Phase 3).
            # Declared here so the finally can always tear them down, even on
            # an early return from Phase 0.5 / 1 / 2.
            child_q: asyncio.Queue | None = None
            echo_pump: asyncio.Task | None = None
            # ── Phase 0.5: redispatch governor (wire-level) ────────────────
            # Fires for ALL callers including bus_trusted — same precedent as
            # the engagement-disabled / pending-question / cross-team / cycle
            # gates above (trust bypasses approval, not correctness). Keyed on
            # the (caller, canonical-target) PAIR only — never the prompt — so
            # relabeling a redispatch as a "new blocker" can't reset it (the
            # offshore-001 failure mode). Registered FIRST (call_id above) so
            # the operator can `bus pending`/`bus cancel` it. ask_partner sets
            # _skip_redispatch_gate (advisory consultation, fully exempt);
            # ask_agents children set _swarm_fanout_approved (governed once at
            # the parent, so they skip here but still count at submit).
            if not flags.skip_redispatch_gate and not flags.swarm_fanout_approved:
                _rd_threshold = daemon._redispatch_threshold()
                _rd_count = daemon._redispatch_check(owner, name)
                if _rd_count >= _rd_threshold - 1:
                    qid, fut_q = daemon.add_redispatch_question(
                        owner,
                        name,
                        prompt,
                        consecutive=_rd_count + 1,
                    )
                    daemon.bus_call_set_future(call_id, fut_q)
                    daemon.bus_call_set_state(call_id, "awaiting_redispatch_gate")
                    # BOUNDED wait — the reaper only nudges 'awaiting_reply'
                    # (see _bus_call_reaper), so this gate state would never be
                    # stall-flagged; never await with timeout=None.
                    # The trailing `or 600.0` floors the wait: prompt_timeout
                    # can be 0 (the operator's documented "disable timeouts"
                    # value), and wait_for(timeout=0) raises TimeoutError on
                    # the next tick — which would auto-fail the gate before
                    # the operator could answer. 600s mirrors the Phase-1
                    # agent-start gate's fixed human window.
                    _rd_timeout = (
                        runner.prompt_timeout
                        if runner is not None and runner.prompt_timeout > 0
                        else daemon.prompt_timeout
                    ) or 600.0
                    try:
                        answer = await asyncio.wait_for(fut_q, timeout=_rd_timeout)
                    except TimeoutError:
                        daemon.inbox.expire(qid, "[timed out]")
                        return _text(
                            f"error: operator did not answer redispatch Q{qid} "
                            f"in time; refusing the redispatch to {name!r}",
                            error=True,
                        )
                    except RuntimeError as e:  # operator cancelled via bus cancel
                        daemon.inbox.expire(qid, f"[cancelled: {e}]")
                        return _text(f"error: {e}", error=True)
                    # Parse a leading 'yes N' LOCALLY — the shared
                    # _parse_delegation_answer reads only the first token (so
                    # it would silently drop the N) and also backs Phase 1/2/
                    # subagent/lesson gates, so the budget is extracted here.
                    _rd_credit = _parse_yes_n(answer)
                    verdict, payload = _parse_delegation_answer(answer)
                    if verdict == "deny":
                        reason = payload or "(no reason given)"
                        return _text(
                            f"operator denied redispatch to {name!r}: {reason}",
                            error=True,
                        )
                    if verdict == "edit":
                        prompt = payload
                        daemon._redispatch_spend_one(owner, name)
                    elif _rd_credit > 1:
                        daemon._redispatch_grant_credit(owner, name, _rd_credit)
                    else:
                        daemon._redispatch_spend_one(owner, name)

            # ── Phase 1: agent-start gate, if target isn't running ─────────
            if runner is None or runner.status == "stopped":
                if name not in daemon.all_cfgs:
                    avail = ", ".join(sorted(daemon.runners)) or "(none)"
                    return _text(
                        f"error: no agent {name!r} configured. available: {avail}",
                        error=True,
                    )
                if _trust_covers(bus_trusted, name):
                    # Trusted-for-this-target callers auto-start without filing
                    # a question — audit the skipped gate (D-1).
                    await _record_approval_bypass(
                        daemon,
                        owner,
                        name,
                        "agent_start",
                        prompt,
                        bus_trusted,
                    )
                    daemon.bus_call_set_state(call_id, "awaiting_agent_start")
                    try:
                        await daemon.start_agent(name)
                    except Exception as e:  # noqa: BLE001
                        return _text(
                            f"error starting {name!r}: {type(e).__name__}: {e}",
                            error=True,
                        )
                    runner = daemon.runners.get(name)
                    if runner is None:
                        return _text(
                            f"error: agent {name!r} did not register after start",
                            error=True,
                        )
                else:
                    qid, fut_q = daemon.add_agent_start_question(owner, name, prompt)
                    daemon.bus_call_set_future(call_id, fut_q)
                    daemon.bus_call_set_state(call_id, "awaiting_agent_start")
                    try:
                        answer = await asyncio.wait_for(fut_q, timeout=600.0)
                    except TimeoutError:
                        daemon.inbox.expire(qid, "[timed out]")
                        return _text(
                            f"error: operator did not answer agent-start Q{qid} "
                            f"in time; {name!r} is still not running",
                            error=True,
                        )
                    except RuntimeError as e:  # operator cancelled via bus cancel
                        daemon.inbox.expire(qid, f"[cancelled: {e}]")
                        return _text(f"error: {e}", error=True)
                    verdict, payload = _parse_delegation_answer(answer)
                    if verdict == "deny":
                        reason = payload or "(no reason given)"
                        return _text(
                            f"operator declined to start {name!r}: {reason}",
                            error=True,
                        )
                    if verdict == "edit":
                        prompt = payload
                    try:
                        await daemon.start_agent(name)
                    except Exception as e:  # noqa: BLE001
                        return _text(
                            f"error starting {name!r}: {type(e).__name__}: {e}",
                            error=True,
                        )
                    runner = daemon.runners.get(name)
                    if runner is None:
                        return _text(
                            f"error: agent {name!r} did not register after start",
                            error=True,
                        )

            # ── Phase 2: delegation approval gate ──────────────────────────
            # Trusted-for-this-target callers skip this entirely; an
            # out-of-list target still hits the operator gate below.
            gate = (caller_cfg.get("policy") or {}).get("approve_before_delegate")
            _trusted_here = _trust_covers(bus_trusted, name)
            if _trusted_here and _delegation_gated(gate, name):
                # Trust skipped a gate the policy WOULD have raised — audit it.
                # (Guard on _delegation_gated so callers with no delegation
                # policy don't record a bypass for a gate that never existed.)
                await _record_approval_bypass(
                    daemon,
                    owner,
                    name,
                    "delegation",
                    prompt,
                    bus_trusted,
                )
            elif not _trusted_here and _delegation_gated(gate, name):
                qid, fut_q = daemon.add_delegation_question(owner, name, prompt)
                daemon.bus_call_set_future(call_id, fut_q)
                daemon.bus_call_set_state(call_id, "awaiting_delegation_gate")
                try:
                    # BOUNDED wait — the reaper only nudges 'awaiting_reply'
                    # (_questions.py), so this 'awaiting_delegation_gate' state
                    # is never stall-flagged; never await with timeout=None.
                    # The trailing `or 600.0` floors the wait: prompt_timeout
                    # can be 0 (the operator's "disable timeouts" value), and
                    # timeout=None would block the caller forever with no reaper
                    # to rescue it. Mirrors the Phase 0.5 / Phase 1 / swarm gates.
                    timeout = (runner.prompt_timeout or daemon.prompt_timeout) or 600.0
                    answer = await asyncio.wait_for(fut_q, timeout=timeout)
                except TimeoutError:
                    daemon.inbox.expire(qid, "[timed out]")
                    return _text(
                        f"error: operator did not answer delegation Q{qid} in time",
                        error=True,
                    )
                except RuntimeError as e:
                    daemon.inbox.expire(qid, f"[cancelled: {e}]")
                    return _text(f"error: {e}", error=True)
                verdict, payload = _parse_delegation_answer(answer)
                if verdict == "deny":
                    reason = payload or "(no reason given)"
                    return _text(f"operator denied delegation: {reason}", error=True)
                if verdict == "edit":
                    prompt = payload

            # The runner captured at the top can go stale: Phase 1 refreshes it
            # after a start, but if Phase 1 was SKIPPED (target already up) an
            # operator stop/kill during the Phase-2 gate wait (or any await
            # above) could have torn it down. Re-fetch and guard so we never
            # dispatch to a dead runner — return an error so the caller's future
            # resolves cleanly instead of hitting a stopped runner.
            runner = daemon.runners.get(name)
            if runner is None or runner.status == "stopped":
                return _text(
                    f"error: agent {name!r} is no longer running (stopped "
                    f"during delegation setup); not dispatching",
                    error=True,
                )

            # ── Phase 3: submit and await target's reply ───────────────────
            fut: asyncio.Future = loop.create_future()
            daemon.bus_call_set_future(call_id, fut)
            daemon.bus_call_set_state(call_id, "awaiting_reply")
            # Sanitize operator infrastructure values out of the prompt
            # body. The receiver still has the engagement profile and can
            # resolve the real values when it's time to invoke a tool;
            # but the delegation prose no longer carries a fully-
            # substituted operator-infrastructure shape.
            # See `_redact_operator_infra` docstring for the why.
            prompt, _redactions = _redact_operator_infra(prompt, daemon)
            if _redactions:
                caller_runner = daemon.runners.get(owner)
                if caller_runner is not None:
                    with suppress(Exception):
                        caller_runner._publish(
                            "bus-redact",
                            "ask_agent → "
                            + name
                            + ": redacted operator infra ("
                            + "; ".join(_redactions)
                            + ")",
                        )
            # Build the delegation envelope. Empty when neither field was
            # set, so simple ask_agent("worker", "summarize record 42") looks
            # identical to before — opt-in tightening, not a default change.
            envelope = _render_delegation_envelope(
                caller=owner,
                max_turns_hint=max_turns_hint,
                deliverable=deliverable,
            )
            wrapped = f"{envelope}\n\n---\n\n{prompt}" if envelope else prompt
            expanded, _subs = daemon.expand_prompt(wrapped)
            # Provenance event: log the incoming peer delegation on the
            # receiver's transcript BEFORE submit AND publish to the
            # receiver's live stream so the web pane shows
            # "[from <caller>] <prompt>" in line with text/tool events.
            # Pre-2026-05-16 this was JSONL-only and never surfaced in
            # the live UI.
            # Observability must never block the dispatch: a disk-full /
            # runner-side logging failure here should not short-circuit the
            # delegation. Mirror the two _publish calls in this function.
            with suppress(Exception):
                await runner._log_provenance(
                    "peer_message",
                    expanded,
                    source=owner,
                    recipient=name,
                    extras={
                        "envelope_caller": owner,
                        "max_turns_hint": max_turns_hint or None,
                        "deliverable": deliverable or None,
                        "substituted_from": substituted_from,
                    },
                )
            # Subscribe to the target's live stream BEFORE submit so no event
            # of our job is missed. Best-effort: the echo is operator
            # observability only (the model never sees it — the SDK has no
            # streaming tool-result type), so a runner that can't be tailed
            # must not break dispatch. Same principle as the suppress-wrapped
            # _publish / _log_provenance above. Snapshot discarded — every
            # snapshot event predates our job, and the pump filters by job_id.
            with suppress(Exception):
                child_q, _snap = runner.subscribe()
            # Thread max_turns_hint onto the Job so the runner can enforce
            # it as a HARD wire-level ceiling — the envelope's prose alone
            # is ignorable (shadows have been observed running to the SDK's
            # internal ~31-turn cap, dangling caller futures until the
            # 1200s prompt-timeout). None when no hint was sent.
            # Per-dispatch skin observer (default no-op). Built before submit so
            # on_submit can hand it the child Job id the moment it exists.
            observer = _make_delegation_observer(daemon, owner, runner)
            child_job = runner.submit(
                expanded,
                future=fut,
                max_turns_hint=(max_turns_hint or None),
                # Pure carry-through of the inert verification-leg bit: dispatch
                # flag → Job stamp. The kernel never reads it; a skin verifier does.
                verification_leg=flags.verification_leg,
            )
            with suppress(Exception):
                observer.on_submit(child_job)
            # In-process write-back sink (same family as the routing flags —
            # never in the public schema): a Python caller that passed a
            # `job_capture` dict on the typed flags channel gets the child Job id
            # written back, so it can scope later event queries to THIS dispatch
            # (ask_consensus uses it to isolate per-leg traces from concurrent
            # jobs). Rides on BusFlags (not args) precisely so the write-back
            # aliases the producer's dict — model_dump on the args model would
            # sever it. None on the wire path, so this is a no-op there.
            if flags.job_capture is not None:
                flags.job_capture["job_id"] = child_job.id
            # Redispatch governor: record this dispatch now that it has
            # actually reached the runner — only here, so a dispatch that
            # died in an earlier phase or was denied at the gate doesn't burn
            # the counter. ask_partner (advisory) is fully exempt; swarm
            # children DO count (so a later single ask_agent to a swarmed
            # child is correctly seen as a redispatch).
            if not flags.skip_redispatch_gate:
                daemon._redispatch_increment(owner, name)
            # Echo + cancel wiring are best-effort enhancements layered on the
            # core dispatch (submit / await / return) — they must never break
            # it. Worst case degrades to today's behavior (no echo; cancel
            # settles the caller's future without stopping the child).
            with suppress(Exception):
                # Record the child Job id so an operator `bus cancel` can
                # interrupt the child runner (cancel_job), not just settle our
                # future — which would leave the child burning tokens.
                daemon.bus_call_set_child_job(call_id, child_job.id)
                # Operator-visible echo: mirror the child's live events onto
                # the caller's pane, isolated to OUR job by job_id. Always on,
                # like bus-substitute / bus-redact. Torn down in finally.
                caller_runner = daemon.runners.get(owner)
                if child_q is not None and caller_runner is not None:
                    echo_pump = asyncio.create_task(
                        _echo_child_stream(
                            child_q,
                            caller_runner,
                            child=name,
                            child_job_id=child_job.id,
                            observer=observer,
                        )
                    )
            # Caller-side timeout. Composed of three signals, take MAX:
            #
            #  (a) Base = target runner's prompt_timeout + 60s slop.
            #      Same shape as before; +60s lets the runner time out
            #      first so the caller hears the rewritten error from
            #      _last_interrupt_reason before our own wait fires
            #      (2026-05-16 fix; SDK subprocess cleanup after
            #      client.interrupt() routinely takes 10-20s).
            #
            #  (b) max_turns_hint scaling. If the caller passed a hint
            #      (the soft per-call budget), assume each turn could
            #      take up to ~60s of wall time (deep disasm, tool
            #      chains, multi-step reasoning) and size the wait
            #      accordingly. Without this, a max_turns=50 child can
            #      easily run past the caller's default 1260s window.
            #
            #  (c) Swarm-orchestrator awareness. If the target is a
            #      `swarm_orchestrator: true` agent, its internal job
            #      is to ask_agents N children — wall time is the
            #      slowest child's own wait + the orchestrator's own
            #      decomposition + synthesis turns. Look at the swarm
            #      composition + each source's swarm_member_max_turns
            #      floor to estimate the longest child path; add the
            #      orchestrator's own turn budget. Pre-fix the bus
            #      gave up at 1260s while mixed-swarm × 12 members was
            #      still mid-fanout — observed 2026-05-21 on a
            #      large mixed swarm.
            #
            # The whole thing is capped at 4h to prevent runaway.
            timeout = _compute_ask_agent_timeout(
                daemon=daemon,
                target_name=name,
                target_runner=runner,
                max_turns_hint=max_turns_hint,
            )
            try:
                job = await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError:
                return _text(
                    f"error: {name} did not reply within wait window",
                    error=True,
                )
            except RuntimeError as e:
                return _text(f"error: {e}", error=True)
            if job.error:
                return _text(f"error from {name}: {job.error}", error=True)
            result = job.result or "(empty reply)"
            if substituted_from is not None:
                # Tell the caller their delegation got routed to a
                # substitute. Useful audit trail and helps the caller
                # decide whether to keep using the substitute (cheaper /
                # different reasoning trace) or start the primary back up.
                result = (
                    f"[routed {substituted_from!r} → {name!r} — "
                    f"substitute is running (cost-tier default; stop "
                    f"{name!r} to force {substituted_from!r})]\n\n"
                    f"{result}"
                )
            # Stop the echo pump and drain any events it hadn't reached, so a
            # registered observer sees the COMPLETE child stream before it
            # finalizes the reply (e.g. a refusal fired near end-of-job). The
            # pump is best-effort operator echo; draining here is observer
            # correctness (mirrors the subscribe→pump→await→drain discipline).
            # With the pump stopped, this coroutine is the sole child_q consumer,
            # so no event is split between the two.
            if echo_pump is not None:
                echo_pump.cancel()
                with suppress(asyncio.CancelledError):
                    await echo_pump
                echo_pump = None
            if child_q is not None:
                while not child_q.empty():
                    drained = child_q.get_nowait()
                    if drained.get("job_id") == child_job.id:
                        with suppress(Exception):
                            observer.record(drained)
            with suppress(Exception):
                result = observer.finalize(result)
            return _text(result)
        finally:
            # Tear down the delegated-stream echo: cancel the pump and drop our
            # subscription so the child's subscriber list doesn't leak. The
            # pump never unsubscribes itself (it may be mid-`get`); the queue
            # lifecycle is owned here. `runner` is the re-fetched target whose
            # stream we subscribed to in Phase 3 (unchanged since).
            if echo_pump is not None:
                echo_pump.cancel()
                with suppress(asyncio.CancelledError):
                    await echo_pump
            if child_q is not None and runner is not None:
                runner.unsubscribe(child_q)
            daemon.bus_call_resolve(call_id)

    @bus_tool(
        "ask_partner",
        "Consult your primary agent for a second opinion. Use this when "
        "you're a shadow agent (your config has `substitute_for: <primary>` "
        "set) and you want the on-harness primary's take on a question. "
        "The call routes to the literal primary — substitute-routing is "
        "bypassed so it doesn't loop back to you. Same operator-gate "
        "model as ask_agent (your policy's `approve_before_delegate` "
        "applies; `bus_trusted: true` short-circuits). Returns the "
        "primary's reply.\n"
        "\n"
        "Refused with a clear message if your config doesn't declare "
        "`substitute_for` (primaries don't have a partner to consult).\n"
        "  prompt — REQUIRED. The question for your primary.\n"
        "  max_turns / deliverable — optional, same semantics as "
        "ask_agent's envelope params.",
        _AskPartnerArgs,
    )
    async def ask_partner(args: dict[str, Any]) -> dict[str, Any]:
        # Caller validation: only shadows have a primary to consult.
        caller_cfg = daemon.all_cfgs.get(owner) or {}
        primary = caller_cfg.get("substitute_for")
        if not primary:
            return _text(
                f"ask_partner refused: caller {owner!r} is not a shadow "
                f"(no `substitute_for` in its config). Primaries don't "
                f"have a partner to consult — they ARE the on-harness "
                f"reference. Use ask_agent if you want to delegate to a "
                f"different agent.",
                error=True,
            )
        # Delegate to ask_agent with substitute-routing bypassed so the
        # call reaches the literal primary, not the shadow itself.
        forwarded_args = {
            "name": primary,
            "prompt": args.get("prompt") or "",
        }
        # Reach the literal primary (not the shadow itself) and, as an advisory
        # shadow→primary consultation, stay exempt from the redispatch governor.
        # ask_partner is model-facing and carries no inbound flags, so build a
        # fresh set rather than copying.
        forwarded_flags = BusFlags(skip_substitute_routing=True, skip_redispatch_gate=True)
        if args.get("max_turns"):
            forwarded_args["max_turns"] = args["max_turns"]
        if args.get("deliverable"):
            forwarded_args["deliverable"] = args["deliverable"]
        # In-process delegation: call ask_agent's .trusted entry (the routed
        # side channel), NOT the model-facing .handler. .trusted passes our
        # typed flags straight through; .handler is wire-only and would force
        # default flags. forwarded_args is a fresh, fixed-key dict (never a
        # model-derived one), so bus_tool's validation is a clean pass.
        return await ask_agent.trusted(forwarded_args, flags=forwarded_flags)

    @bus_tool(
        "ask_agents",
        "Swarm fan-out: dispatch DIFFERENT sub-tasks to N child agents "
        "concurrently, then synthesize their replies into one unified "
        "answer. Each child runs through the normal ask_agent path "
        "(same gates, cycle detection, substitute routing) so safeguards "
        "compose.\n"
        "\n"
        "═══ HOW TO USE THIS TOOL CORRECTLY ═══\n"
        "\n"
        "1. DECOMPOSE the work BEFORE you call. Each child's `prompt` "
        "   MUST be a DIFFERENT slice of the larger task — a different "
        "   subquestion, target, file, time window, or angle. If you "
        "   find yourself writing the same prompt twice, you are using "
        "   the wrong tool — use `ask_agent` instead, or rethink the "
        "   decomposition.\n"
        "\n"
        "   GOOD (decomposed): one child probes the network layer, "
        "   another probes the web layer, a third reviews the auth "
        "   layer — each gets a prompt scoped to its slice + the "
        "   specialist best suited for it.\n"
        "\n"
        "   BAD (duplicate work): sending 'What does your role do?' "
        "   to three agents, or copy-pasting the same investigation "
        "   prompt to N specialists. That wastes turns + tokens and "
        "   produces overlapping, hard-to-merge output.\n"
        "\n"
        "2. PICK SPECIALISTS deliberately. Match each slice to the "
        "   agent whose role fits it. Use `list_agents` first if you "
        "   are unsure who covers what. Don't fan out to agents that "
        "   will produce near-identical replies.\n"
        "\n"
        "3. AFTER results return, SYNTHESIZE — don't dump JSON back to "
        "   the operator. Read every child reply, reconcile "
        "   contradictions, merge into ONE coherent answer in your "
        "   own voice. The structured result is YOUR raw material; "
        "   your reply is the finished product.\n"
        "\n"
        "═══ Inputs ═══\n"
        "  children — REQUIRED. List of {name, prompt, max_turns?, "
        "             deliverable?, prefer_primary?} objects, 1..20. "
        "             Each `prompt` MUST be different.\n"
        "  aggregate — 'all' (gather all N), 'any' (return "
        "             first to complete), 'race' (return first AND "
        "             cancel siblings).\n"
        "  concurrency — Max children in flight at once. Throttle for "
        "             LiteLLM-proxied endpoint shadows.\n"
        "  deliverable — Optional shared deliverable string joined into "
        "             every child's envelope alongside its own slice-"
        "             specific prompt.\n"
        "\n"
        "Output: { ok, aggregate, parent_call_id, results: [{call_id, "
        "name, ok, result|error, substituted_from?}], "
        "cancelled_siblings?: [call_id,...] }. The `results` list is "
        "INPUT to your synthesis step — not output for the operator.\n"
        "\n"
        "Cycle detection: if ANY proposed child would close a delegation "
        "cycle (caller → ... → caller), the WHOLE call is refused with "
        "the cycle path. Re-call with the offending name(s) removed.\n"
        "\n"
        "Operator approval: if any child's target is gated by your "
        "policy.approve_before_delegate, a single batched question is "
        "filed listing all gated children. Operator answers once with "
        "yes / no / edit:<shared prompt> / only:<comma list>.",
        _AskAgentsArgs,
    )
    async def ask_agents(args: dict[str, Any]) -> dict[str, Any]:
        from ..coord.delegation_graph import find_cycles_for_edges, format_cycle

        # Caller-side restriction: agents flagged `restrict_swarm_tools:
        # true` in agents.yaml (typically local-LLM test surfaces like
        # `ollama`) cannot orchestrate swarms. The LiteLLM/Ollama shim
        # doesn't reliably preserve parallel-tool-call semantics, and
        # those agents exist for chat-and-bus integration testing, not
        # production swarm work. Refuse with a clear error so the
        # operator sees why and can edit the agent config if they
        # genuinely want swarm capability there.
        caller_cfg = daemon.all_cfgs.get(owner) or {}
        if caller_cfg.get("restrict_swarm_tools"):
            return _text(
                f"error: ask_agents refused — caller {owner!r} has "
                f"`restrict_swarm_tools: true` in agents.yaml. This agent "
                f"is a local-LLM test surface that must not orchestrate "
                f"swarms. If you need swarm capability here, flip the "
                f"flag off in agents.yaml deliberately + add a per-agent "
                f"test. Until then: use `ask_agent` for single-target "
                f"delegations instead.",
                error=True,
            )

        # Model-guaranteed: a list of 1..20 child objects (min_length/max_length).
        children = args["children"]
        # Validate each child has name + prompt; collect a deduped name set
        # so we can refuse "spawn N copies of the same agent" early.
        # Also normalize prompts so we can detect duplicate-prompt fan-outs
        # (decomposition smell — see warnings emission below).
        seen_names: set[str] = set()
        prompt_to_children: dict[str, list[str]] = {}
        for i, ch in enumerate(children):
            if not isinstance(ch, dict):
                return _text(f"error: children[{i}] must be an object", error=True)
            cn = (ch.get("name") or "").strip()
            cp = ch.get("prompt") or ""
            if not cn or not cp:
                return _text(
                    f"error: children[{i}] requires both name and prompt",
                    error=True,
                )
            if cn == owner:
                return _text(
                    f"error: children[{i}] is the caller itself ({owner!r}) — "
                    f"self-fanout would deadlock",
                    error=True,
                )
            if cn in seen_names:
                return _text(
                    f"error: children[{i}] name {cn!r} duplicated in the "
                    f"fan-out; each child must be a distinct agent (v1)",
                    error=True,
                )
            # Callee-side restriction: agents flagged `restrict_swarm_tools`
            # can't be swarm CHILDREN either. Mirrors the caller-side
            # refusal above. Same rationale: local-LLM agents are test
            # surfaces and should be reached only via direct ask_agent,
            # never as one of N parallel siblings.
            child_cfg = daemon.all_cfgs.get(cn) or {}
            if child_cfg.get("restrict_swarm_tools"):
                return _text(
                    f"error: children[{i}] target {cn!r} has "
                    f"`restrict_swarm_tools: true` in agents.yaml — it's "
                    f"a local-LLM test surface that can't participate in "
                    f"swarms. Remove {cn!r} from the children list, OR "
                    f"send it work via a direct ask_agent call (which is "
                    f"unrestricted).",
                    error=True,
                )
            seen_names.add(cn)
            # Normalize prompt for duplicate detection: lowercase, collapse
            # whitespace, strip. Catches "What does your role do?" vs.
            # "  what does your role do?\n" as the same content.
            norm = " ".join((cp or "").lower().split()).strip()
            if norm:
                prompt_to_children.setdefault(norm, []).append(cn)

        # Soft warnings — surfaced in the result payload so the model sees
        # them in the same turn and self-corrects on the next call. NOT
        # refusals: there are legitimate uses for same-prompt fan-out
        # (e.g. diverse-perspective polling), but it's a strong code smell
        # and the operator's intent for this tool is decomposed work.
        swarm_warnings: list[str] = []
        dup_clusters = [names for names in prompt_to_children.values() if len(names) > 1]
        if dup_clusters:
            if len(dup_clusters) == 1 and len(dup_clusters[0]) == len(children):
                # All N children share one prompt — the strongest signal of
                # missing decomposition.
                swarm_warnings.append(
                    "All children received IDENTICAL prompts. The purpose of "
                    "ask_agents is parallel work on DIFFERENT slices of a "
                    "larger task — not duplicate dispatch. Next time: "
                    "decompose the task and give each child its own slice, "
                    "or use ask_agent for a single dispatch."
                )
            else:
                groups = "; ".join(f"[{', '.join(names)}]" for names in dup_clusters)
                swarm_warnings.append(
                    f"Some children shared identical prompts: {groups}. Each "
                    f"child should normally get a DIFFERENT sub-task. If you "
                    f"only needed one reply, drop the duplicates; if you "
                    f"need diverse perspectives on the same question, that's "
                    f"a valid use but consider whether the prompt is the "
                    f"right shape to elicit different angles."
                )

        aggregate = args["aggregate"]  # model-validated enum: all | any | race

        # Concurrency cap: caller picks, bounded by N. None ⇒ auto = min(N, 10);
        # an explicit value is already 1..20 (le=20 enforced by the model), so we
        # only still clamp to N here (a cross-field bound ge=/le= can't express).
        N = len(children)
        concurrency = args["concurrency"]
        if concurrency is None:
            concurrency = min(N, 10)
        concurrency = min(concurrency, N)

        shared_deliverable = args["deliverable"].strip()

        # Cycle detection — check ALL proposed child edges against the
        # current in-flight graph BEFORE registering anything. If any
        # would close a cycle, refuse the whole call (per design doc:
        # partial dispatch produces subtle wrong synthesize outputs).
        existing_calls = list((daemon._bus_calls or {}).values())
        child_names = [ch["name"].strip() for ch in children]
        # Resolve aliases on each name so cycle detection sees real names
        # (matches what ask_agent will substitute later).
        from ..alias import to_real as _alias_to_real

        resolved_names = [_alias_to_real(n) for n in child_names]
        cycle_check = find_cycles_for_edges(
            existing_calls,
            owner,
            resolved_names,
        )
        cycles_found = [(n, path) for n, path in cycle_check.items() if path is not None]
        if cycles_found:
            _log.info(
                "ask_agents refused fan-out: %d of %d children would close "
                "a cycle (caller=%s, offenders=%s)",
                len(cycles_found),
                N,
                owner,
                ", ".join(f"{name}:{format_cycle(path)}" for name, path in cycles_found),
            )
            lines = [
                f"error: ask_agents refused — {len(cycles_found)} of "
                f"{N} children would close a delegation cycle.",
                "",
            ]
            for name, path in cycles_found:
                lines.append(f"  • {name}: {format_cycle(path)}")
            lines.extend(
                [
                    "",
                    "Refusing the WHOLE fan-out so partial dispatch can't "
                    "produce subtly-wrong synthesize outputs. Re-call with "
                    "the offending name(s) removed.",
                ]
            )
            return _text("\n".join(lines), error=True)

        # Register the synthetic parent BusCall. swarm_role="parent" so
        # the tree renderer + bus_call_cancel cascade pivot on it. Its
        # future is a placeholder we never await; the cancel cascade
        # operates on children's futures.
        loop = asyncio.get_running_loop()
        parent_future = loop.create_future()
        parent_call_id = daemon.bus_call_register(
            owner,
            f"<swarm:{N}>",
            f"ask_agents → {N} children",
            parent_future,
            swarm_role="parent",
            initial_state="awaiting_fanout",
        )

        # Endpoint-throttling semaphore: per-host shared semaphore so a
        # 20-wide swarm of DeepSeek shadows doesn't slam the single
        # LiteLLM proxy. The registry is eager-initialized on the daemon
        # (see core.py __init__). Default 8 concurrent per host.
        def _endpoint_sem_for(name: str) -> asyncio.Semaphore | None:
            cfg = daemon.all_cfgs.get(name) or {}
            ep = cfg.get("endpoint") or {}
            base = ep.get("base_url") if isinstance(ep, dict) else None
            if not base:
                return None
            sem: asyncio.Semaphore | None = daemon._endpoint_semaphores.get(base)
            if sem is None:
                sem = asyncio.Semaphore(8)
                daemon._endpoint_semaphores[base] = sem
            return sem

        # Per-swarm concurrency cap via a local semaphore.
        swarm_sem = asyncio.Semaphore(concurrency)

        async def _run_child(idx: int, child: dict) -> dict:
            """Wrap ask_agent for one child. Returns a result dict the
            aggregator collects. Errors are captured (not raised) so
            sibling children can still complete under aggregate=all."""
            name = (child.get("name") or "").strip()
            child_args = {
                "name": name,
                "prompt": child.get("prompt") or "",
            }
            # The whole fan-out is governed ONCE at the parent (the swarm
            # fan-out gate below), so children unconditionally skip the
            # per-child redispatch gate — otherwise a pre-tripped child pair
            # would file its own question concurrently with the batched one
            # (double-gating). Children STILL increment their own per-pair
            # counter at submit. parent_call_id lets the child's BusCall be
            # cancelled when the parent swarm tears down.
            child_flags = BusFlags(parent_call_id=parent_call_id, swarm_fanout_approved=True)
            if child.get("max_turns"):
                child_args["max_turns"] = child["max_turns"]
            # Stitch in the shared deliverable + per-child deliverable.
            d_parts = []
            if shared_deliverable:
                d_parts.append(shared_deliverable)
            if child.get("deliverable"):
                d_parts.append(child["deliverable"])
            if d_parts:
                child_args["deliverable"] = "\n".join(d_parts)
            if child.get("prefer_primary"):
                child_args["prefer_primary"] = True

            endpoint_sem = _endpoint_sem_for(_alias_to_real(name))
            # In-process dispatch of one swarm child: call ask_agent's .trusted
            # entry so the typed child_flags (parent_call_id + swarm_fanout_
            # approved) reach the routed body. .handler is wire-only.
            try:
                async with swarm_sem:
                    if endpoint_sem is not None:
                        async with endpoint_sem:
                            reply = await ask_agent.trusted(child_args, flags=child_flags)
                    else:
                        reply = await ask_agent.trusted(child_args, flags=child_flags)
            except asyncio.CancelledError:
                # Race-mode sibling cancel arrives as CancelledError on
                # the inner ask_agent's wait_for. Surface as a tidy
                # cancelled result, not a leaked exception.
                return {
                    "name": name,
                    "ok": False,
                    "error": "cancelled (sibling raced ahead)",
                    "_cancelled": True,
                }
            except Exception as exc:  # noqa: BLE001 — bus-tool fault wall
                return {"name": name, "ok": False, "error": str(exc)}
            # Unwrap the _text envelope ask_agent returns (shared with
            # _consensus to keep the error-key handling single-sourced —
            # _text writes snake_case `is_error`, so reading only `isError`
            # here silently swallowed every returned-envelope child error).
            ok, text = _unwrap(reply)
            return {"name": name, "ok": ok, "result" if ok else "error": text}

        # ── Swarm fan-out gate (wire-level) ────────────────────────────────
        # Multi-target fan-out is inherently an escalation. Gate the WHOLE
        # fan-out behind ONE operator answer when: the caller is trusted for
        # >= swarm_min of the children (the offshore-001 surface), OR any child is
        # gated by the caller's approve_before_delegate policy, OR any child
        # pair is already redispatch-tripped (closes the single-child
        # "swarm laundering" bypass — wrapping a repeat as a 1-child
        # ask_agents). This finally implements the batched-question contract
        # promised in this tool's docstring. Children always carry
        # _swarm_fanout_approved (set above) so they don't re-gate per-child.
        _rd_threshold = daemon._redispatch_threshold()
        # Count only children the caller is trusted FOR — a list-scoped
        # orchestrator's out-of-list children fall through to their own
        # nested per-child Phase 1/2 gates (and recorders), so the batched
        # fan-out question governs only the trusted subset. Byte-equivalent
        # to the old `bool(bus_trusted)` for `true` (== N) and falsy (== 0).
        _bus_trusted_cfg = caller_cfg.get("bus_trusted")
        _trusted_children = sum(1 for rn in resolved_names if _trust_covers(_bus_trusted_cfg, rn))
        _swarm_min = daemon._redispatch_swarm_min()
        _policy_gate = (caller_cfg.get("policy") or {}).get("approve_before_delegate")
        _any_child_policy_gated = any(_delegation_gated(_policy_gate, rn) for rn in resolved_names)
        _any_child_tripped = any(
            daemon._redispatch_check(owner, rn) >= _rd_threshold - 1 for rn in resolved_names
        )
        # Single outer try so the synthetic parent BusCall is resolved on
        # EVERY exit — including a CancelledError raised while awaiting the
        # fan-out gate (stop(kill=True) / race-cancel). The reaper only
        # nudges 'awaiting_reply', so a leaked 'awaiting_fanout_gate' row
        # would otherwise sit in `bus pending` until daemon shutdown.
        try:
            if (_trusted_children >= _swarm_min) or _any_child_policy_gated or _any_child_tripped:
                qid, fut_q = daemon.add_swarm_fanout_question(owner, resolved_names)
                daemon.bus_call_set_future(parent_call_id, fut_q)
                daemon.bus_call_set_state(parent_call_id, "awaiting_fanout_gate")
                # BOUNDED wait (the reaper only nudges 'awaiting_reply'); the
                # `or 600.0` floors it so prompt_timeout=0 ("disable timeouts")
                # doesn't auto-fail the gate on the next tick.
                try:
                    answer = await asyncio.wait_for(
                        fut_q,
                        timeout=(daemon.prompt_timeout or 600.0),
                    )
                except TimeoutError:
                    daemon.inbox.expire(qid, "[timed out]")
                    return _text(
                        f"error: operator did not answer the swarm fan-out "
                        f"Q{qid} in time; refusing the fan-out",
                        error=True,
                    )
                except RuntimeError as e:  # operator cancelled via bus cancel
                    daemon.inbox.expire(qid, f"[cancelled: {e}]")
                    return _text(f"error: {e}", error=True)
                # Parse yes / no [reason] / edit:<shared> / only:<subset>.
                _fa = (answer or "").strip()
                _fa_low = _fa.lower()
                if _fa_low.startswith(("only:", "only ")):
                    _subset_raw = (
                        _fa.split(None, 1)[1]
                        if _fa_low.startswith("only ")
                        else _fa[len("only:") :]
                    )
                    _subset = {
                        s.strip().lower()
                        for s in _subset_raw.replace(",", " ").split()
                        if s.strip()
                    }
                    kept = [
                        ch
                        for ch in children
                        if (ch.get("name") or "").strip().lower() in _subset
                        or _alias_to_real((ch.get("name") or "").strip()).lower() in _subset
                    ]
                    if not kept:
                        return _text(
                            f"error: 'only:' subset {sorted(_subset)} matched "
                            f"no children of this fan-out; nothing dispatched",
                            error=True,
                        )
                    children = kept
                    N = len(children)
                else:
                    verdict, payload = _parse_delegation_answer(answer)
                    if verdict == "deny":
                        reason = payload or "(no reason given)"
                        return _text(
                            f"operator refused the swarm fan-out: {reason}",
                            error=True,
                        )
                    if verdict == "edit":
                        for ch in children:
                            ch["prompt"] = payload
                # Approved — return the parent to its normal in-flight state
                # so `bus pending` reads correctly while children run.
                daemon.bus_call_set_state(parent_call_id, "awaiting_fanout")

            # Dispatch. Each child is an asyncio.Task so we can selectively
            # cancel siblings on race.
            tasks: list[asyncio.Task] = []
            for i, child in enumerate(children):
                t = asyncio.create_task(_run_child(i, child))
                tasks.append(t)

            cancelled_siblings: list[str] = []
            if aggregate == "all":
                # Always returns N results; per-child ok flag carries
                # success/failure.
                gathered = await asyncio.gather(
                    *tasks,
                    return_exceptions=True,
                )
                results: list[dict[str, Any]] = []
                for r in gathered:
                    if isinstance(r, Exception):
                        results.append({"name": "?", "ok": False, "error": str(r)})
                    else:
                        results.append(cast("dict[str, Any]", r))
                return _text(
                    _format_swarm_payload(
                        parent_call_id,
                        aggregate,
                        results,
                        warnings=swarm_warnings or None,
                    ),
                )

            if aggregate in ("any", "race"):
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                winner = next(iter(done))
                winner_result = winner.result()
                if aggregate == "race":
                    # Cancel the pending siblings. v1 cancels the FUTURE
                    # only via bus_call_cancel — the inner ask_agent's
                    # await will see RuntimeError. Don't interrupt the
                    # runners themselves yet (v2).
                    daemon.bus_call_cancel(parent_call_id=parent_call_id)
                    for p in pending:
                        p.cancel()
                    cancelled_siblings = [
                        children[i].get("name") for i, t in enumerate(tasks) if t in pending
                    ]
                # any: leave pending tasks running, their results are discarded
                payload = _format_swarm_payload(
                    parent_call_id,
                    aggregate,
                    [winner_result],
                    cancelled_siblings=cancelled_siblings,
                    warnings=swarm_warnings or None,
                )
                return _text(payload)
        finally:
            # Parent row is no longer needed — drop it on EVERY exit (gate
            # error/deny, dispatch return, or a CancelledError propagating
            # through). Children rows were resolved by their own ask_agent
            # finalisers. Idempotent.
            daemon.bus_call_resolve(parent_call_id)

        # Unreachable — aggregate validated above
        return _text("error: unreachable aggregate path", error=True)

    @bus_tool(
        "ask_operator",
        "Use this when you need a CLARIFYING ANSWER from the human operator "
        "before you can proceed (missing target, ambiguous instruction, etc.). "
        "The question is added to the operator's question inbox with a Q-id. "
        "End your turn after calling this tool — the operator will reply via "
        "`salientctl answer <q-id> <text>` which arrives as your next prompt.",
        _AskOperatorArgs,
    )
    async def ask_operator(args: dict[str, Any]) -> dict[str, Any]:
        question = (args.get("question") or "").strip()
        if not question:
            return _text("error: question is required", error=True)
        qid = daemon.add_question(owner, question)
        return _text(
            f"Question Q{qid} queued for the operator. "
            f"End your turn now; their reply will arrive as your next prompt."
        )

    return [ask_agent, ask_agents, ask_partner, ask_operator]
