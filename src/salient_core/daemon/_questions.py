"""Daemon mixin: operator-question gate + inter-agent bus-call tracking.

Methods extracted from salient/daemon/core.py to keep the central
Daemon class navigable. All methods continue to access `self.X` exactly
as before — Daemon assembles them via multiple inheritance.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from contextlib import suppress
from typing import Any, cast

from ..coord.questions import Question
from ..display import _emit
from ._event_hub import _EventObservationMixin
from ._helpers import _CONTEXT_VAR_RE, BusCall, Job, _wrap_context_value
from ._tasks import spawn_background
from .runner import AgentRunner

_log = logging.getLogger("salient.daemon.bus_calls")


# ── Operator-authz provider seam ─────────────────────────────────────
# `_cmd_note_resolve` scopes a non-owner operator out of another's note
# unless their role outranks "viewer". WHO the operators are and WHAT roles
# they hold is deployment/skin data (a web-users config), not kernel
# mechanism — so the kernel ships no authz and defaults to a NULL config
# (no records → the viewer restriction never fires → permissive, exactly as
# when a skin's authz config file is absent). A downstream skin registers
# its real ``get_config`` via ``set_authz_provider(...)``; consulted at CALL
# time, same injection shape as the other seams. The provider need only
# duck-type ``.exists() -> bool`` and ``.user_record(op) -> dict | None``.
class _NullAuthzConfig:
    def exists(self) -> bool:
        return False

    def user_record(self, username: str) -> dict[str, Any] | None:
        return None


def _null_authz_get_config() -> Any:
    return _NullAuthzConfig()


_authz_get_config: Any = _null_authz_get_config


def set_authz_provider(get_config: Any) -> None:
    """Register a skin's operator-authz ``get_config`` callable. It returns a
    config object exposing ``.exists()`` and ``.user_record(op)``. Default is
    a null config (no records, permissive)."""
    global _authz_get_config
    _authz_get_config = get_config


# Runaway-delegation backstops (see bus_call_admission_check). Generous
# enough that normal multi-phase engagements never hit them; low enough that a
# pathological deep ladder or exponential fan-out can't nest/saturate without
# bound. The composable fan-out budget (BUS_FANOUT_AND_DEADLOCK.md) is the
# fuller solution; these caps are the sanctioned backstop until then.
MAX_DELEGATION_DEPTH = 8
MAX_INFLIGHT_BUS_CALLS = 256

# Operator replies to a kind="bus_stall" question that mean "abort the call".
# Anything else (including silence / garbage) is treated as "wait" so a
# fat-fingered reply can never destroy in-flight work.
_STALL_CANCEL_WORDS = frozenset(
    {
        "cancel",
        "abort",
        "kill",
        "stop",
        "no",
        "n",
        "nope",
    }
)


def _parse_stall_answer(text: str) -> str:
    """Map an operator's stall-question reply to "cancel" or "wait".

    Cancel only on an explicit cancel word as the first token; default to
    "wait" otherwise so ambiguous/empty answers never cancel."""
    tokens = (text or "").strip().lower().split()
    if not tokens:
        return "wait"
    first = tokens[0].rstrip(",.:;!?")
    return "cancel" if first in _STALL_CANCEL_WORDS else "wait"


class _QuestionsMixin(_EventObservationMixin):
    def expand_prompt(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """Expand `{{agent}}` / `{{agent.key}}` placeholders into context_value
        markers. Returns (expanded_text, list_of_(agent, key)_substitutions).
        """
        substitutions: list[tuple[str, str]] = []

        def repl(m: re.Match) -> str:
            agent = m.group(1)
            key = m.group(2) or "latest"
            value = self.context.read(agent, key)
            substitutions.append((agent, key))
            return _wrap_context_value(agent, key, value)

        return _CONTEXT_VAR_RE.sub(repl, text), substitutions

    def add_question(
        self,
        agent: str,
        text: str,
        job_id: int | None = None,
        *,
        kind: str = "operator",
    ) -> int:
        """Record a question explicitly (called by the `ask_operator` MCP tool).

        Returns the new Q-id. If `job_id` is None, infers it from the agent's
        currently-running job so the tool call can be linked back. `kind`
        defaults to "operator" (answer becomes the agent's next prompt); the
        reaper passes kind="bus_stall" so its answer drives a mechanical
        cancel instead (see _cmd_questions_answer).
        """
        runner = self.runners.get(agent)
        if job_id is None and runner and runner.current is not None:
            job_id = runner.current.id
        q: Question = self.inbox.add(agent, text, job_id=job_id or 0, kind=kind)
        if runner and runner.current is not None:
            runner.current.tool_question_ids.append(q.id)
        self._announce_question(q, source="tool")
        return q.id

    def add_operator_note(
        self,
        from_op: str,
        text: str,
        to_op: str | None = None,
    ) -> int:
        """File an operator-to-operator note + broadcast it to every
        connected operator's Q-inbox (questions_tail subscribers). No runner
        involved — notes are pure operator coordination."""
        q: Question = self.inbox.add_note(from_op, text, to_op)
        target = f" → {to_op}" if to_op else ""
        body = (
            f"📝 NOTE N{q.id} from {from_op}{target}: {q.text}\n"
            f"   → acknowledge with: salientctl note-resolve {q.id}"
        )
        try:
            loop = asyncio.get_running_loop()
            spawn_background(_emit(q.agent, "note", body), loop=loop)
        except RuntimeError:
            print(f"[{q.agent}] note: {body}", flush=True)
        self.inbox.publish("new", q)
        return q.id

    def add_suggestion(self, source: str, text: str) -> int:
        """File a copilot suggestion (assessment idea 4.11) + broadcast it to
        every connected operator surface (questions_tail subscribers). ADVISORY
        only: ephemeral, no runner involved, no future, never blocks. Surfaces
        branch on kind=='suggestion' to count/render it apart from blocking
        questions. Returns the new S-id."""
        q: Question = self.inbox.add_suggestion(source, text)
        body = (
            f"💡 SUGGESTION S{q.id} from {source}: {q.text}\n"
            f"   → dismiss with: salientctl suggestion dismiss {q.id}"
        )
        try:
            loop = asyncio.get_running_loop()
            spawn_background(_emit(q.agent, "suggestion", body), loop=loop)
        except RuntimeError:
            print(f"[{q.agent}] suggestion: {body}", flush=True)
        self.inbox.publish("new", q)
        return q.id

    async def _cmd_suggestion_dismiss(self, req: dict[str, Any]) -> dict[str, Any]:
        """Dismiss copilot suggestions (the advisory analogue of note_resolve).
        Pass ids=[...] for specific suggestions or all=true to clear the lot.
        Marks them answered (so they drop from the inbox) and broadcasts
        'answered' so every surface clears them live."""
        op = req.get("_operator")
        dismiss_all = bool(req.get("all"))
        raw_ids = req.get("ids") or ([] if dismiss_all else None)
        if raw_ids is None and not dismiss_all:
            return {"error": "specify ids=[...] or all=true"}
        if dismiss_all:
            targets = [q for q in self.inbox.list() if q.kind == "suggestion"]
        else:
            targets = []
            for raw in raw_ids or []:
                try:
                    sid = int(raw)
                except (TypeError, ValueError):
                    continue
                q = self.inbox.get(sid)
                if q is not None and q.kind == "suggestion" and not q.answered:
                    targets.append(q)
        dismissed: list[int] = []
        for q in targets:
            self.inbox.mark_answered(q.id, "dismissed", answer_job_id=0, answered_by=op)
            self.inbox.publish("answered", q)
            dismissed.append(q.id)
        return {"ok": True, "dismissed": dismissed, "count": len(dismissed)}

    async def _cmd_shoulder_surf_toggle(self, req: dict[str, Any]) -> dict[str, Any]:
        """Enable/disable the shoulder_surf operator copilot (assessment idea
        4.11). "Enabled" == the manual_only shoulder_surf runner is up — the
        background driver is inert whenever the runner is stopped, so on/off is
        just start/stop (no agent restart, no prefs write). Omit `on` to query
        the current state."""
        r = self.runners.get("shoulder_surf")
        running = r is not None and r.status != "stopped"
        if "on" not in req:
            return {"ok": True, "enabled": running, "state": "running" if running else "stopped"}
        if bool(req.get("on")):
            if "shoulder_surf" not in self.all_cfgs:
                return {"error": "no shoulder_surf agent in config"}
            if not running:
                try:
                    await self.start_agent("shoulder_surf")
                except ValueError as e:
                    return {"error": str(e)}
            return {"ok": True, "enabled": True, "state": "running"}
        if running:
            await r.stop(kill=False)
            self._persist_running_agents()
        return {"ok": True, "enabled": False, "state": "stopped"}

    async def _cmd_note_add(self, req: dict[str, Any]) -> dict[str, Any]:
        text = (req.get("text") or "").strip()
        if not text:
            return {"error": "note text is required"}
        from_op = req.get("_operator") or "local"
        to_op = (req.get("to") or "").strip() or None
        qid = self.add_operator_note(from_op, text, to_op)
        return {"ok": True, "id": qid}

    async def _cmd_note_resolve(self, req: dict[str, Any]) -> dict[str, Any]:
        qid = req.get("id")
        q = self.inbox.get(qid)
        if q is None or q.kind != "note":
            return {"error": f"no operator note #{qid}"}
        if q.answered:
            return {"error": f"note #{qid} already resolved"}
        op = req.get("_operator")
        # Cooperative scoping: a listed VIEWER may resolve only notes they
        # filed or that are addressed to them ("answer questions they
        # filed"). Operators / admins / trusted-local may resolve any.
        if op is not None:
            filed_by = q.agent.split(":", 1)[1] if ":" in q.agent else q.agent
            if op != filed_by and op != q.to_operator:
                cfg = _authz_get_config()
                rec = cfg.user_record(op) if cfg.exists() else None
                if rec is not None and (rec.get("role") or "viewer") == "viewer":
                    return {
                        "error": (
                            f"note #{qid} is not yours: viewers may only resolve "
                            f"notes they filed or addressed to them"
                        )
                    }
        self.inbox.mark_answered(q.id, "resolved", answer_job_id=0, answered_by=op)
        self.inbox.publish("answered", q)
        return {"ok": True, "id": qid}

    def _file_gate_question(
        self, caller: str, text: str, *, source: str
    ) -> tuple[int, asyncio.Future]:
        """File a 'gate' question whose operator answer resolves a future
        rather than becoming the asking agent's next prompt. Both delegation
        approvals and start-agent prompts use this — the asking agent is
        paused inside a tool call and needs the answer threaded back into
        the awaiting `await` rather than queued as a fresh prompt."""
        runner = self.runners.get(caller)
        job_id = runner.current.id if (runner and runner.current is not None) else 0
        q = self.inbox.add(caller, text, job_id=job_id, kind="delegation")
        if runner and runner.current is not None:
            runner.current.tool_question_ids.append(q.id)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.inbox.register_pending(q.id, fut)
        self._announce_question(q, source=source)
        return q.id, fut

    def add_delegation_question(
        self, caller: str, target: str, prompt: str
    ) -> tuple[int, asyncio.Future]:
        """File an operator-approval question for an `ask_agent` delegation."""
        text = (
            f"delegate to {target!r}: {prompt}\n\n"
            f"reply: 'yes' to forward, 'no [reason]' to deny, "
            f"or 'edit: <new prompt>' to modify before forwarding"
        )
        return self._file_gate_question(caller, text, source="delegation")

    def add_agent_start_question(
        self, caller: str, target: str, prompt: str
    ) -> tuple[int, asyncio.Future]:
        """File an operator-approval question for starting a configured-but-
        not-running agent that another agent wants to delegate to."""
        text = (
            f"agent {target!r} is configured but NOT running — "
            f"{caller} wants to delegate to it:\n\n"
            f"  {prompt}\n\n"
            f"reply: 'yes' to start {target} and forward the prompt, "
            f"'no [reason]' to refuse, or 'edit: <new prompt>' to start "
            f"and forward an edited prompt"
        )
        return self._file_gate_question(caller, text, source="agent_start")

    def add_subagent_spawn_question(
        self,
        caller: str,
        subagent_type: str,
        prompt: str,
    ) -> tuple[int, asyncio.Future]:
        """File an operator-approval question for spawning an SDK subagent
        via the Agent/Task tool.

        Added 2026-05-18 as part of the bus-redesign work: the operator
        wanted EVERY subagent spawn to require their approval — even from
        bus_trusted orchestrators (which is stricter than `approve_before_
        delegate`, where bus_trusted bypasses). The hook in runner_factory
        (`_make_subagent_approval_hook`) calls this, awaits the future,
        and translates the operator's answer into a SDK PreToolUse
        decision (allow / deny / edited prompt)."""
        preview = prompt.strip().splitlines()[0] if prompt.strip() else ""
        if len(preview) > 200:
            preview = preview[:199] + "…"
        text = (
            f"{caller} wants to spawn SDK subagent {subagent_type!r}.\n\n"
            f"Subagent prompt preview:\n  {preview}\n\n"
            f"reply: 'yes' to allow the spawn, 'no [reason]' to refuse, "
            f"or 'edit: <new prompt>' to allow with a modified prompt. "
            f"Every subagent spawn requires your approval — even from "
            f"bus_trusted orchestrators."
        )
        return self._file_gate_question(caller, text, source="subagent_spawn")

    def add_tool_approval_question(
        self,
        caller: str,
        tool_label: str,
        summary: str,
        categories: list[str],
    ) -> tuple[int, asyncio.Future]:
        """File an operator-approval question for a tool call whose action-class
        matches the caller's ``policy.approve_before``.

        The wire-level cousin of ``add_delegation_question``: the asking agent is
        paused inside a PreToolUse hook (``_make_approve_before_hook``) waiting on
        the returned future. Goes through ``_file_gate_question`` (kind
        ``delegation``) so ``_cmd_questions_answer`` resolves the future via the
        existing gate path — no answer-handler change. ``summary`` is the
        operator-facing one-liner of what the agent intends (command / key args);
        the inbox is operator-facing, so unlike the model-facing safeguard text it
        may name the action plainly."""
        cats = ", ".join(categories)
        text = (
            f"{caller} wants to run {tool_label} (operator-gated: {cats}).\n\n"
            f"  {summary}\n\n"
            f"reply: 'yes' to allow, 'no [reason]' to refuse, or "
            f"'edit: <new command>' to allow with a modified command."
        )
        return self._file_gate_question(caller, text, source="tool_approval")

    def add_lesson_proposal_question(
        self,
        caller: str,
        text: str,
        kind: str,
    ) -> tuple[int, asyncio.Future]:
        """File an operator-approval question for an agent-proposed LESSON
        (cross-engagement procedural memory). Like add_subagent_spawn_question,
        the operator's answer resolves a future the `propose_lesson` bus tool
        awaits — nothing is written to lessons/<agent>.md without an explicit
        approve/edit (preserving lessons.py's 'agent reads, operator writes'
        invariant)."""
        preview = text.strip()
        if len(preview) > 300:
            preview = preview[:299] + "…"
        qtext = (
            f"{caller} proposes a {kind} LESSON for its future self:\n\n"
            f"  {preview}\n\n"
            f"reply: 'yes' to save it to lessons/{caller}.md, 'no [reason]' to "
            f"decline, or 'edit: <text>' to save a revised version. Agent-"
            f"proposed lessons are operator-gated — nothing is saved on "
            f"deny/timeout."
        )
        return self._file_gate_question(caller, qtext, source="lesson_proposal")

    def add_redispatch_question(
        self,
        caller: str,
        target: str,
        prompt: str,
        *,
        consecutive: int,
    ) -> tuple[int, asyncio.Future]:
        """File an operator-approval question for a REDISPATCH — the 2nd+
        consecutive ask_agent dispatch from `caller` to the same `target`
        with no intervening operator answer.

        Fires EVEN for bus_trusted callers (manager/leads) — the wire
        backstop for the prompt-only redispatch ceiling. Keyed on the
        (caller, target) PAIR, never the prompt text, so relabeling the task
        as a 'new/distinct blocker' does not reset it (the node-01
        failure mode). See memory project_operator_gates_prompt_only.md."""
        preview = prompt.strip().splitlines()[0] if prompt.strip() else ""
        if len(preview) > 200:
            preview = preview[:199] + "…"
        text = (
            f"REDISPATCH GATE: {caller} wants to dispatch {target!r} AGAIN "
            f"(consecutive dispatch #{consecutive} to this target, no "
            f"intervening operator answer). The prompt text may differ but "
            f"the target/objective is the same — relabeling does not reset "
            f"this gate. This fires even from bus_trusted orchestrators.\n\n"
            f"Prompt preview:\n  {preview}\n\n"
            f"reply: 'yes' to allow ONE more (gate re-arms), 'yes N' to "
            f"pre-authorize N dispatches before re-gating, 'no [reason]' to "
            f"stop the chain, or 'edit: <new prompt>' to allow one modified."
        )
        return self._file_gate_question(caller, text, source="redispatch")

    def add_swarm_fanout_question(
        self,
        caller: str,
        child_names: list[str],
    ) -> tuple[int, asyncio.Future]:
        """File ONE batched operator-approval question for an ask_agents
        fan-out. Multi-target fan-out is inherently an escalation, so the
        WHOLE swarm is gated behind a single operator answer rather than N
        per-child questions. Fires even from bus_trusted orchestrators."""
        listing = ", ".join(repr(n) for n in child_names)
        text = (
            f"SWARM FAN-OUT GATE: {caller} wants to fan out to "
            f"{len(child_names)} agents: {listing}. Multi-target dispatch is "
            f"an escalation and is gated even from bus_trusted orchestrators."
            f"\n\nreply: 'yes' to dispatch all, 'no [reason]' to refuse the "
            f"whole fan-out, 'edit: <shared prompt>' to override every "
            f"child's prompt, or 'only: a, b' to dispatch just that subset."
        )
        return self._file_gate_question(caller, text, source="swarm_fanout")

    def bus_call_register(
        self,
        caller: str,
        target: str,
        prompt: str,
        future: asyncio.Future,
        *,
        parent_call_id: int | None = None,
        swarm_role: str | None = None,
        initial_state: str = "awaiting_agent_start",
    ) -> int:
        """Register an in-flight ask_agent call. Returns a call_id the bus
        tool uses to update state and resolve on exit.

        ``parent_call_id`` / ``swarm_role`` (added 2026-05-18) wire a row
        into a fan-out hierarchy for ask_agents:
          - regular ask_agent: both default None → behaves as before.
          - ask_agents child:  parent_call_id = the synthetic parent's id.
          - synthetic parent:  swarm_role = "parent", parent_call_id None,
                               target is a label like "<swarm:N>" and the
                               initial state is "awaiting_fanout".
        """
        call_id: int = self._next_bus_call_id
        self._next_bus_call_id += 1
        preview = prompt.strip().splitlines()[0] if prompt.strip() else ""
        if len(preview) > 120:
            preview = preview[:119] + "…"
        self._bus_calls[call_id] = BusCall(
            id=call_id,
            caller=caller,
            target=target,
            prompt_preview=preview,
            started_at=time.time(),
            state=initial_state,
            future=future,
            parent_call_id=parent_call_id,
            swarm_role=swarm_role,
            depth=self._bus_call_depth_for(caller),
        )
        return call_id

    def _bus_call_depth_for(self, caller: str) -> int:
        """Delegation depth the NEXT call from ``caller`` will have: one more
        than the deepest in-flight call whose ``target`` is ``caller`` (the
        call that put ``caller`` to work). A root delegation — ``caller`` is
        not currently a delegation target — is depth 0."""
        deepest = -1
        for c in self._bus_calls.values():
            if c.target == caller and c.depth > deepest:
                deepest = c.depth
        return deepest + 1

    def bus_call_admission_check(self, caller: str) -> str | None:
        """Backstop gate for runaway delegation, checked BEFORE register.
        Returns a human-readable refusal reason, or None to admit. Bounds two
        failure modes the composable fan-out budget would also address:
        unbounded delegation DEPTH (deep ladders / nested fan-out) and total
        in-flight CALL COUNT (wide pathological swarms exhausting memory)."""
        if len(self._bus_calls) >= MAX_INFLIGHT_BUS_CALLS:
            return (
                f"error: too many in-flight bus calls "
                f"({len(self._bus_calls)} ≥ {MAX_INFLIGHT_BUS_CALLS}). The "
                f"delegation graph is saturated — wait for outstanding calls "
                f"to return, or ask the operator to cancel stuck ones "
                f"(`bus pending` / `bus cancel`) before delegating more."
            )
        depth = self._bus_call_depth_for(caller)
        if depth > MAX_DELEGATION_DEPTH:
            return (
                f"error: delegation too deep (depth {depth} > "
                f"{MAX_DELEGATION_DEPTH}). This call is {depth} levels down a "
                f"delegation chain; refusing to nest further. Do the work in "
                f"your lane or file an ask_operator instead of delegating on."
            )
        return None

    def bus_call_set_state(self, call_id: int, state: str) -> None:
        call = self._bus_calls.get(call_id)
        if call is not None:
            call.state = state

    def bus_call_set_future(self, call_id: int, future: asyncio.Future) -> None:
        """Swap the tracked future when ask_agent transitions phases. Cancel
        operates on whichever future is current at the moment of cancel.

        INVARIANT: the PREVIOUS future is intentionally not settled by this
        method. That's safe today because every ask_agent phase awaits its
        future via `asyncio.wait_for(...)` and then calls this method with
        the next phase's future before awaiting again — no coroutine is
        ever awaiting an old future at the moment it's replaced. If a
        future maintainer adds a code path that holds a reference to a
        phase's future past its phase boundary (e.g. attaching a
        done-callback then transitioning), this invariant breaks and the
        old future will leak. Either settle the old future here (cancel
        or set_exception) before swapping, or document why the new code
        path is also safe under the existing contract.
        """
        call = self._bus_calls.get(call_id)
        if call is not None:
            call.future = future

    def bus_call_set_child_job(self, call_id: int, job_id: int) -> None:
        """Record the child runner's Job id on the BusCall (set at Phase 3
        submit). Lets `bus_call_cancel` interrupt the child runner via
        `cancel_job` rather than only settling the caller's future."""
        call = self._bus_calls.get(call_id)
        if call is not None:
            call.child_job_id = job_id

    def _clear_stall_question(self, call: BusCall, note: str) -> None:
        """Auto-resolve a dangling kind='bus_stall' question when its call
        ends by a path OTHER than the operator answering it (the target
        finally replied, or an explicit `bus cancel`). Keeps the inbox from
        showing a stall question for a call that's already gone. No-op when
        the call has no pending stall question (e.g. the operator's own
        'cancel' answer nulls stall_qid before triggering the cancel)."""
        qid = call.stall_qid
        if qid is None:
            return
        call.stall_qid = None
        q = self.inbox.get(qid)
        if q is None or q.answered:
            return
        self.inbox.mark_answered(qid, note, answer_job_id=0)
        self.inbox.publish("answered", q)

    def bus_call_resolve(self, call_id: int) -> None:
        """Drop a call from the registry (called by the bus tool on every
        return path — success or error). Idempotent."""
        call = self._bus_calls.pop(call_id, None)
        if call is not None:
            self._clear_stall_question(call, "auto: bus call completed")

    def bus_call_cancel(
        self,
        caller: str | None = None,
        target: str | None = None,
        *,
        parent_call_id: int | None = None,
        call_id: int | None = None,
    ) -> list[int]:
        """Resolve every matching pending call's future with a cancellation
        error so the asking agent's await unblocks. Returns the cancelled
        call ids. All filters optional — empty matches everything (caller
        confirmation should happen at the CLI layer).

        ``parent_call_id`` (added 2026-05-18 for ask_agents): match every
        row whose parent_call_id == this value. Use to cascade-cancel all
        children of a swarm parent.

        ``call_id`` (added 2026-05-18): match a single row by its id. Use
        for "cancel just this child" (e.g. ``aggregate=race`` sibling
        cancel) or to target the swarm parent explicitly.

        CASCADE: when a cancelled row has its OWN children (rows whose
        ``parent_call_id`` equals the cancelled row's id), those children
        are cancelled too. Bounded: call ids monotonically increase, so
        the recursion can't loop. This is what makes "cancel the swarm
        parent" propagate to all children with one operator action.
        """
        # Snapshot initial matches; cascade is applied below so children
        # of a matched parent get swept even when not in the filter set.
        cancelled: list[int] = []
        matched_ids: set[int] = set()
        for cid, call in list(self._bus_calls.items()):
            if call_id is not None and cid != call_id:
                continue
            if caller and call.caller != caller:
                continue
            if target and call.target != target:
                continue
            if parent_call_id is not None and call.parent_call_id != parent_call_id:
                continue
            matched_ids.add(cid)

        # Cascade two ways so cancelling one call tears down everything it
        # spawned, in one operator action:
        #   (a) SWARM children — rows whose parent_call_id == the matched id
        #       (ask_agents fan-out).
        #   (b) PLAIN LADDER descendants — rows whose caller == the matched
        #       row's target, i.e. the callee's own outstanding delegations
        #       (A→B→C: cancelling A→B also cancels B→C…). Without this, plain
        #       ask_agent ladders left the tail hanging until its own timeout
        #       (BUS_DESIGN_AUDIT.md). We sweep the callee's DOWNSTREAM calls
        #       only — other inbound calls to that callee (different chains)
        #       are untouched. Bounded: matched_ids only grows over a finite
        #       call set, and cycles are refused at submit, so this terminates.
        to_process: list[int] = list(matched_ids)
        while to_process:
            pid = to_process.pop()
            parent = self._bus_calls.get(pid)
            parent_target = parent.target if parent is not None else None
            for cid, call in self._bus_calls.items():
                if cid in matched_ids:
                    continue
                is_swarm_child = call.parent_call_id == pid
                is_ladder_child = parent_target is not None and call.caller == parent_target
                if is_swarm_child or is_ladder_child:
                    matched_ids.add(cid)
                    to_process.append(cid)

        for cid in matched_ids:
            call = self._bus_calls.get(cid)
            if call is None:
                continue
            # Abandoned-on-restart calls have future=None — just drop them.
            if call.future is None:
                self._clear_stall_question(call, "auto: abandoned call dismissed")
                self._bus_calls.pop(cid, None)
                cancelled.append(cid)
                continue
            if call.future.done():
                self._clear_stall_question(call, "auto: bus call already settled")
                self._bus_calls.pop(cid, None)
                continue
            call.cancelled = True
            call.future.set_exception(
                RuntimeError(
                    f"bus call cancelled by operator (caller={call.caller}, "
                    f"target={call.target}, state={call.state})"
                )
            )
            # Settling the future above only unblocks the CALLER's await. If
            # this call reached Phase 3 dispatch (child_job_id set), also stop
            # the child runner so it isn't left burning tokens. Fire-and-forget
            # — the cancel path must not block on SDK interrupt latency.
            jid = call.child_job_id
            if jid is not None:
                runner = self.runners.get(call.target)
                if runner is not None:
                    with suppress(Exception):
                        asyncio.create_task(runner.cancel_job(jid))
            self._clear_stall_question(call, "auto: bus call cancelled by operator")
            self._bus_calls.pop(cid, None)
            cancelled.append(cid)
        return cancelled

    # ── Redispatch governor (wire-level consecutive-dispatch gate) ─────────
    # Counts consecutive ask_agent dispatches per (caller, canonical-target)
    # pair since the last operator answer, so a runaway redispatch chain
    # (node-01: a bus_trusted manager self-authorizing ~10 in a row by
    # relabeling each) trips an operator question even though bus_trusted
    # skips Phase 1/2. Keyed on the PAIR only — never the prompt — so
    # relabeling can't reset it. See salient/bus/_delegation.py ask_agent.

    def _redispatch_key(self, caller: str, target: str) -> tuple[str, str]:
        """Canonical (caller, target) key. Collapses a substitute to its
        `substitute_for` PRIMARY so e.g. a substitute and its primary share
        one bucket — closes the split-key bypass where alternating
        prefer_primary / stopping the substitute would otherwise launder
        dispatches into two first-free counters."""
        from ..alias import to_real as _alias_to_real

        t = _alias_to_real((target or "").strip())
        cfg = self.all_cfgs.get(t) or {}
        canonical = cfg.get("substitute_for") or t
        return (caller, canonical)

    def _redispatch_threshold(self) -> int:
        from ..policy import safeguards

        return safeguards.redispatch_threshold_from_profile(getattr(self, "profile", None))

    def _redispatch_swarm_min(self) -> int:
        from ..policy import safeguards

        return safeguards.redispatch_swarm_min_from_profile(getattr(self, "profile", None))

    def _redispatch_check(self, caller: str, target: str) -> int:
        """Current value for the pair: >=0 consecutive count, <0 remaining
        pre-authorized 'yes N' credit. Applies the opt-in idle-TTL — a pair
        untouched longer than reset_idle_seconds is treated as fresh (0).
        No mutation beyond lazy TTL expiry."""
        from ..policy import safeguards

        key = self._redispatch_key(caller, target)
        idle = safeguards.redispatch_idle_seconds_from_profile(getattr(self, "profile", None))
        if idle > 0:
            last = self._redispatch_last_ts.get(key)
            if last is not None and (time.time() - last) > idle:
                self._redispatch_counts.pop(key, None)
                self._redispatch_last_ts.pop(key, None)
                return 0
        return cast("int", self._redispatch_counts.get(key, 0))

    def _redispatch_increment(self, caller: str, target: str) -> None:
        """Record one actual dispatch to the pair. Called once, at the moment
        the dispatch reaches runner.submit() — NOT at gate-pass or register —
        so a dispatch that dies in an earlier phase or is denied does not
        burn the counter. A negative 'yes N' credit climbs toward 0 with the
        same +1 step; once it crosses the threshold the gate re-arms."""
        key = self._redispatch_key(caller, target)
        self._redispatch_counts[key] = self._redispatch_counts.get(key, 0) + 1
        self._redispatch_last_ts[key] = time.time()

    def _redispatch_spend_one(self, caller: str, target: str) -> None:
        """Operator approved ONE more redispatch. Set the count to
        threshold-1 so the dispatch that follows (incrementing at submit)
        lands back AT the gate threshold — the VERY NEXT redispatch re-trips.
        'Spend-one, re-arm': every redispatch past the first is a deliberate
        operator decision."""
        key = self._redispatch_key(caller, target)
        self._redispatch_counts[key] = max(self._redispatch_threshold() - 1, 0)
        self._redispatch_last_ts[key] = time.time()

    def _redispatch_grant_credit(self, caller: str, target: str, n: int) -> None:
        """Operator approved a bounded budget of N redispatches ('yes N').
        Store -(N-1) so this dispatch plus N-1 more pass the gate before it
        re-arms (each dispatch's +1 at submit climbs the negative credit
        toward 0). N<=1 degrades to spend-one."""
        if n <= 1:
            self._redispatch_spend_one(caller, target)
            return
        key = self._redispatch_key(caller, target)
        self._redispatch_counts[key] = -(n - 1)
        # An operator answer IS a touch of the pair — refresh the idle clock
        # so a slow deliberation doesn't let the opt-in TTL discard the
        # freshly-granted credit before it's used.
        self._redispatch_last_ts[key] = time.time()

    def _bus_stall_threshold(self, stall_multiplier: float = 3.0) -> float:
        """Seconds a bus call may sit in `awaiting_reply` before the reaper
        flags it stalled.

        Operator override via the engagement profile's
        `rate.bus_call_timeout_seconds` (an absolute wall-clock ceiling —
        engagements with a known short max task runtime set this low so a
        wedged call surfaces in minutes, not `prompt_timeout × 3`). Falls
        back to `prompt_timeout × stall_multiplier` when unset, non-numeric,
        or non-positive."""
        rate = (getattr(self, "profile", None) or {}).get("rate") or {}
        override = rate.get("bus_call_timeout_seconds")
        if override is not None:
            try:
                val = float(override)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
        return float(self.prompt_timeout) * stall_multiplier

    async def _embedding_backfill_once(self, *, limit: int = 200) -> None:
        """Embed KG facts missing a current-model vector, in one bounded batch.
        Inert (no-op) when no embedder is configured; never raises — semantic
        recall is an optional enhancement, not load-bearing. Driven off the
        reaper loop so embedding cost stays off the agent hot path."""
        from ..memory.embeddings import get_embedder, pack_vector

        embedder = get_embedder(getattr(self, "profile", None))
        if embedder is None:
            return
        pending = self.kg.facts_needing_embedding(embedder.model, limit=limit)
        if not pending:
            return
        vecs = await embedder.embed([text for _, text in pending])
        if not vecs:
            return
        self.kg.store_embeddings(
            [(fid, pack_vector(v)) for (fid, _), v in zip(pending, vecs, strict=False)],
            embedder.model,
        )

    async def _bus_call_reaper(
        self,
        *,
        interval: float = 30.0,
        stall_multiplier: float = 3.0,
    ) -> None:
        """Periodic watchdog over `_bus_calls`. An entry sitting in
        `awaiting_reply` for longer than the stall threshold
        (`_bus_stall_threshold` — operator-overridable via
        `rate.bus_call_timeout_seconds`, else `prompt_timeout ×
        stall_multiplier`) is treated as stalled — the target agent is
        wedged and the caller's future is going to hang the full
        prompt_timeout window unless someone acts. The reaper files an
        operator question on the caller's chain so the stall becomes
        visible immediately rather than buried inside the bus's await
        window.

        Closes failure-modes #3 + #5 in docs/COMM_PATHS.md. Companion
        to the silent-completion nudge in runner.py — together they
        ensure both a "did nothing" and a "hung mid-flight" agent
        surface to the operator instead of strand the chain.

        `flagged_stalled` on the BusCall prevents re-firing the same
        question every interval. The flag clears when the call resolves
        (entry leaves `_bus_calls`) or when the daemon shuts down."""
        import time as _time

        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                now = _time.time()
                threshold = self._bus_stall_threshold(stall_multiplier)
                # Describe the threshold's basis for the operator: an
                # absolute `rate.bus_call_timeout_seconds` override reads
                # differently from the prompt_timeout multiple default.
                default_threshold = float(self.prompt_timeout) * stall_multiplier
                if abs(threshold - default_threshold) > 1e-6:
                    basis = "operator-set rate.bus_call_timeout_seconds"
                else:
                    basis = f"{stall_multiplier:g}× prompt_timeout"
                for call in list(self._bus_calls.values()):
                    if call.flagged_stalled:
                        continue
                    if call.state != "awaiting_reply":
                        continue
                    if now < call.stall_snooze_until:
                        continue  # operator said "wait" — re-ask later
                    age = now - call.started_at
                    if age < threshold:
                        continue
                    call.flagged_stalled = True
                    _log.warning(
                        "bus call #%d stalled %ds caller=%s target=%s (threshold %ds, %s)",
                        call.id,
                        int(age),
                        call.caller,
                        call.target,
                        int(threshold),
                        basis,
                    )
                    text = (
                        f"bus call #{call.id} STALLED: {call.caller!r} "
                        f"→ {call.target!r} has been awaiting reply "
                        f"for {int(age)}s (threshold {int(threshold)}s, "
                        f"{basis}). The "
                        f"target appears wedged.\n\n"
                        f"  task: {call.prompt_preview!r}\n\n"
                        f"reply 'cancel' to abort this call (the caller "
                        f"unblocks with a cancellation error) or 'wait' to "
                        f"keep waiting (I'll re-check and ask again if it's "
                        f"still stalled). You can also run "
                        f"`salientctl bus cancel --id {call.id}`, or restart "
                        f"{call.target!r} via `salientctl restart`."
                    )
                    # kind="bus_stall": the answer drives a mechanical cancel,
                    # NOT a queued prompt the blocked caller can't consume.
                    call.stall_qid = self.add_question(call.caller, text, kind="bus_stall")
                # Opportunistic semantic-memory backfill: embed any KG facts
                # lacking a current-model vector (bounded per pass; inert when
                # no embedder configured). Inside the try so an embedding error
                # is caught + logged like any other reaper hiccup.
                await self._embedding_backfill_once()
            except Exception as e:
                # Reaper must not die from a single bad call — log and
                # keep polling. The operator can `salientctl bus pending`
                # to see what's stuck if the reaper itself is broken.
                _log.exception("bus reaper iteration failed: %r", e)
                try:
                    loop = asyncio.get_running_loop()
                    spawn_background(
                        _emit("daemon", "warn", f"bus reaper error: {e!r}"),
                        loop=loop,
                    )
                except RuntimeError:
                    pass

    # ------------------------------------------------------------------ #
    # shoulder_surf — operator copilot (assessment idea 4.11)
    # ------------------------------------------------------------------ #
    # Kinds the copilot cares about. Routine text/tool-call/tool-result chatter
    # is excluded so the digest stays focused and the opus spend stays bounded.
    _SURF_SALIENT_KINDS = frozenset(
        {
            "operator_answer",
            "question",
            "refusal",
            "hard-cap",
            "nudge",
            "tool-error",
            "stopped",
        }
    )
    _SURF_BUSY_SECONDS = 60.0  # an agent working this long is worth flagging
    _SURF_DONE_COST_USD = 0.05  # a single turn costing this much is notable

    def _shoulder_surf_self_names(self) -> set[str]:
        """shoulder_surf + any shadow of it. The driver must never digest its
        own events (it produces text/thinking/done on the same EventHub) or it
        would feed itself — a suggestion would trigger another suggestion."""
        names = {"shoulder_surf"}
        for n, cfg in self.all_cfgs.items():
            if (cfg or {}).get("substitute_for") == "shoulder_surf":
                names.add(n)
        return names

    def _surf_is_salient(self, evt: dict[str, Any], self_names: set[str]) -> bool:
        if evt.get("agent") in self_names:
            return False
        kind = evt.get("kind")
        if kind in self._SURF_SALIENT_KINDS:
            return True
        if kind == "done":
            usage = (evt.get("meta") or {}).get("usage") or {}
            try:
                return float(usage.get("cost_usd") or 0.0) >= self._SURF_DONE_COST_USD
            except (TypeError, ValueError):
                return False
        return False

    def _surf_busy_agents(self, self_names: set[str]) -> list[tuple[str, int, str]]:
        """Snapshot of agents that have been working on the current job for a
        while — the 'manager has been thinking 90s' signal, which no single
        event carries."""
        now = time.time()
        out: list[tuple[str, int, str]] = []
        for name, r in self.runners.items():
            if name in self_names:
                continue
            cur = getattr(r, "current", None)
            if cur is None:
                continue
            started = getattr(cur, "started_at", None)
            if not started:
                continue
            elapsed = now - started
            if elapsed >= self._SURF_BUSY_SECONDS:
                preview = (getattr(cur, "prompt", "") or "").strip().replace("\n", " ")
                out.append((name, int(elapsed), preview[:160]))
        return out

    def _build_surf_prompt(
        self,
        salient: list[dict[str, Any]],
        busy: list[tuple[str, int, str]],
    ) -> str:
        lines = ["=== Recent engagement activity (digest) ==="]
        for e in salient[-40:]:
            kind = e.get("kind")
            agent = e.get("agent")
            text = (e.get("text") or "").strip().replace("\n", " ")
            if len(text) > 200:
                text = text[:200] + "…"
            meta = e.get("meta") or {}
            extra = f" [qid={meta.get('qid')}]" if meta.get("qid") is not None else ""
            lines.append(f"- [{kind}] {agent}{extra}: {text}")
        if busy:
            lines.append("")
            lines.append("=== Agents currently working (long-running) ===")
            for name, elapsed, preview in busy:
                lines.append(f"- {name}: working {elapsed}s on: {preview}")
        lines.append("")
        lines.append(
            "Per your contract, surface at most a few high-value, time-sensitive "
            "suggestions as <suggest>...</suggest> lines, or reply exactly NONE."
        )
        return "\n".join(lines)

    @staticmethod
    def _surf_parse(reply: str) -> list[str]:
        if not reply or reply.strip().upper() == "NONE":
            return []
        out: list[str] = []
        for m in re.findall(r"<suggest>(.*?)</suggest>", reply, re.DOTALL | re.IGNORECASE):
            t = " ".join(m.split()).strip()
            if t and t.upper() != "NONE":
                out.append(t[:400])
        return out

    async def _shoulder_surf_driver(self, *, interval: float = 75.0) -> None:
        """Periodic copilot loop (assessment idea 4.11). Digests recent
        engagement activity and files ADVISORY suggestions for the operator.

        OFF BY DEFAULT: the shoulder_surf agent is `manual_only`, so the loop is
        inert until the operator runs `shoulder-surf on` (or `start
        shoulder_surf`). Cost is bounded by: off-by-default, the poll interval,
        a single-in-flight guard, the agent's `max_turns: 1`, and a content
        debounce (no salient events + no long-running agent → no prompt).

        Modeled on `_bus_call_reaper`: one resilient loop that never dies on a
        single bad iteration. Advisory-only — it must never wedge the daemon."""
        try:
            queue, _snapshot = self.subscribe_events()
        except Exception:  # noqa: BLE001 — no hub → nothing to watch
            return
        seen_order: deque[str] = deque(maxlen=64)
        seen: set[str] = set()
        try:
            while True:
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return
                try:
                    await self._shoulder_surf_pass(queue, seen_order, seen)
                except Exception as e:  # noqa: BLE001 — keep polling
                    _log.exception("shoulder_surf driver iteration failed: %r", e)
        finally:
            with suppress(Exception):
                self.unsubscribe_events(queue)

    async def _shoulder_surf_pass(
        self,
        queue: asyncio.Queue,
        seen_order: deque[str],
        seen: set[str],
    ) -> None:
        # Always drain the bounded subscription so it can't wedge full of stale
        # events while the copilot is off — even when we discard this window.
        drained: list[dict[str, Any]] = []
        while True:
            try:
                drained.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        r = self.runners.get("shoulder_surf")
        if r is None or r.status == "stopped" or r.current is not None:
            return  # off, or already chewing on a digest (single-in-flight)
        self_names = self._shoulder_surf_self_names()
        salient = [e for e in drained if self._surf_is_salient(e, self_names)]
        busy = self._surf_busy_agents(self_names)
        if not salient and not busy:
            return  # content debounce: nothing changed → don't spend
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        r.submit(
            self._build_surf_prompt(salient, busy),
            future=fut,
            suppress_banner=True,
            max_turns_hint=1,
        )
        try:
            job = await asyncio.wait_for(fut, timeout=float(r.prompt_timeout))
        except Exception:  # noqa: BLE001 — timeout/error: skip this window
            return
        reply = (getattr(job, "result", None) or "").strip()
        for text in self._surf_parse(reply):
            norm = text.lower()
            if norm in seen:
                continue  # already surfaced recently — don't repeat
            if len(seen_order) == seen_order.maxlen:
                seen.discard(seen_order[0])
            seen_order.append(norm)
            seen.add(norm)
            self.add_suggestion("shoulder_surf", text)

    def _file_question(self, agent: str, job_id: int, text: str, source: str) -> Question:
        """File a question lifted from a `<ask_operator>` marker in a reply."""
        q: Question = self.inbox.add(agent, text, job_id=job_id)
        self._announce_question(q, source=source)
        return q

    def _announce_question(self, q: Question, source: str) -> None:
        """Make a new question visible everywhere we can:
        - high-visibility banner on daemon stdout (asyncio task to use _emit)
        - synthetic event on the agent's tail stream
        - push to global questions_tail subscribers (salientctl REPL clients)
        """
        runner = self.runners.get(q.agent)
        body = (
            f"⚠ NEW QUESTION Q{q.id} from {q.agent} "
            f"(via {source}): {q.text}\n"
            f"   → reply with: salientctl answer {q.id} <text>"
        )
        if runner is not None:
            # Inject into the runner's pubsub channel so any tail subscriber
            # gets a colored "question" event. Also adds to the ring buffer.
            runner._publish("question", body)
        # Daemon stdout banner (separate from the agent's own stream).
        # We're inside a sync callback path; schedule the print on the loop.
        try:
            loop = asyncio.get_running_loop()
            spawn_background(_emit(q.agent, "question", body), loop=loop)
        except RuntimeError:
            # No running loop (called from outside async context); fall back
            # to plain print so the banner still appears on stdout.
            print(f"[{q.agent}] question: {body}", flush=True)
        self.inbox.publish("new", q)

    def _publish_reply_event(self, runner: AgentRunner, job: Job) -> None:
        """Push a job-completion event to every replies_tail subscriber.

        Suppression is opt-in via `job.suppress_banner` — set ONLY when the
        operator's CLI invocation will print the reply itself (i.e.
        `prompt --wait` / `answer --wait`). Replies driven by another
        agent's `ask_agent` call DO get banners — the operator should see
        all agent activity in the REPL, including delegated work.
        """
        if not job.result and job.error is None:
            return
        if job.suppress_banner:
            return
        evt = {
            "event": "reply",
            "agent": runner.name,
            "task_id": job.id,
            "result": job.result or "",
            "error": job.error,
            "duration": (job.finished_at or 0) - (job.started_at or 0),
            "ts": time.time(),
        }
        for sub in list(self._reply_subs):
            try:
                sub.put_nowait(evt)
            except asyncio.QueueFull:
                pass

    async def _cmd_questions_list(self, req: dict[str, Any]) -> dict[str, Any]:
        show_all = bool(req.get("all"))
        qs = [self.inbox.to_payload(q) for q in self.inbox.list(include_answered=show_all)]
        return {"questions": qs}

    def _maybe_teardown_settled_swarms(self, agents: set[str]) -> None:
        """Schedule teardown for any ephemeral swarm that just lost its
        last pending question via ``questions clear``.

        The deferred auto-teardown (_swarm_should_defer_teardown) only
        re-fires when a job COMPLETES — answering a question re-dispatches
        the orchestrator and produces such a job, but *clearing* one does
        not. Without this, an ephemeral swarm whose question was cleared
        (rather than answered) would idle alive forever (runners + registry
        entry leaked). For each just-cleared agent, find the ephemeral
        swarm it belongs to (orchestrator or member); if that swarm now
        owes no answer across ``[orch, *members]``, schedule its teardown.
        _swarm_teardown is idempotent, so a double-schedule is harmless."""
        swarms = getattr(self, "_swarms", {}) or {}
        if not swarms or not agents:
            return
        scheduled: set[str] = set()
        for orch, entry in list(swarms.items()):
            if not entry.get("ephemeral") or orch in scheduled:
                continue
            group = (orch, *(entry.get("members") or []))
            if not any(a in group for a in agents):
                continue
            if any(self.inbox.pending_for(n) for n in group):
                continue  # still owes the operator an answer
            scheduled.add(orch)
            spawn_background(
                self._swarm_teardown(orch, reason="questions cleared"),
                name=f"swarm-teardown[{orch}]",
            )

    async def _cmd_questions_clear(self, req: dict[str, Any]) -> dict[str, Any]:
        """Clear (mark-answered without a real reply) pending questions.
        Selectors are mutually exclusive — pass exactly one:

          ids: list[int]   — clear specific question IDs
          agent: str       — clear all pending for that agent name
          orphaned: bool   — clear all pending whose filing agent is
                             not currently a runner (the common case
                             after a daemon restart where some agents
                             didn't come back online — e.g. forked
                             lead-team variants like `red_lead`)
          all: bool        — clear ALL pending questions (broad reset)

        Delegation-kind questions have their pending future resolved
        with a deny-flavored text so awaiting tool calls don't hang.
        Returns {ok, cleared: [qid...], count}."""
        explicit_ids = req.get("ids") or []
        agent_filter = (req.get("agent") or "").strip()
        orphaned = bool(req.get("orphaned"))
        clear_all = bool(req.get("all"))
        reason = (req.get("reason") or "cleared by operator").strip() or "cleared by operator"

        targets: list = []
        # Already-answered questions the operator EXPLICITLY named by id —
        # almost always a gate question that timed out / was bus-cancelled and
        # slipped through as a phantom (a lagging subscriber dropped its
        # 'answered' event, or it predates the publish-on-expire fix). There's
        # nothing left to clear, but re-publishing 'answered' makes every live
        # surface drop it — this is what makes the web "delete" button (and the
        # TUI dismiss key) authoritative for timed-out questions.
        phantoms: list = []
        if explicit_ids:
            for raw in explicit_ids:
                try:
                    qid = int(raw)
                except (TypeError, ValueError):
                    continue
                q = self.inbox.get(qid)
                if q is None:
                    continue
                if q.answered:
                    phantoms.append(q)
                else:
                    targets.append(q)
        elif agent_filter:
            targets = [q for q in self.inbox.list() if q.agent == agent_filter]
        elif orphaned:
            running = set(self.runners.keys())
            targets = [q for q in self.inbox.list() if q.agent not in running]
        elif clear_all:
            targets = list(self.inbox.list())
        else:
            return {
                "error": "specify one of: ids=[...], agent=<name>, orphaned=true, all=true",
            }

        # Resolve any pending delegation futures FIRST so the awaiting
        # caller-agent tool calls don't hang. Use a deny-flavored text so
        # the caller's <answer> envelope clearly shows "no" semantics.
        deny_text = f"deny: {reason}"
        for q in targets:
            pending = self.inbox.pop_pending(q.id)
            if pending is not None and not pending.done():
                try:
                    pending.set_result(deny_text)
                except Exception:  # noqa: BLE001
                    pass

        qids = [q.id for q in targets]
        cleared = self.inbox.clear(qids, reason=reason)
        for q in cleared:
            self.inbox.publish("answered", q)

        # Drop any explicitly-named phantoms from every live surface (see the
        # `phantoms` note above). Idempotent at the subscriber: 'answered'
        # just removes the id from the cached snapshot if still present.
        for q in phantoms:
            self.inbox.publish("answered", q)

        # If this emptied an ephemeral swarm's last pending question, tear
        # it down now — the deferred auto-teardown only re-fires on a
        # completed job, which `clear` (unlike `answer`) never produces.
        self._maybe_teardown_settled_swarms({q.agent for q in cleared})

        dismissed = [q.id for q in cleared] + [q.id for q in phantoms]
        return {
            "ok": True,
            "cleared": dismissed,
            "count": len(dismissed),
        }

    async def _cmd_questions_answer(self, req: dict[str, Any]) -> dict[str, Any]:
        qid = req.get("id")
        text = req.get("text") or ""
        # Declared operator identity (multi-operator attribution). Left in
        # req by _dispatch; None for a trusted-local operator.
        op = req.get("_operator")
        q = self.inbox.get(qid)
        if q is None:
            return {"error": f"no question #{qid}"}
        if q.answered:
            return {"error": f"question #{qid} already answered"}
        # Advisory suggestions are not answerable — they would otherwise submit
        # the operator's text as a prompt to shoulder_surf. Steer to dismiss.
        if q.kind == "suggestion":
            return {
                "error": (
                    f"#{qid} is an advisory suggestion, not a question — dismiss it "
                    f"with `suggestion dismiss {qid}`"
                )
            }
        runner = self.runners.get(q.agent)
        if runner is None:
            return {"error": f"agent {q.agent!r} no longer running"}
        # Bus-stall questions: the caller is blocked inside ask_agent awaiting
        # the BusCall future, so an answer must drive a MECHANICAL action
        # (cancel the call / keep waiting), NOT become a queued prompt the
        # blocked agent can't consume. "cancel" resolves the BusCall future
        # with a cancellation error (the ask_agent await unblocks); "wait"
        # re-arms the reaper after one more stall window.
        if q.kind == "bus_stall":
            call = next((c for c in self._bus_calls.values() if c.stall_qid == q.id), None)
            if _parse_stall_answer(text) == "cancel":
                if call is not None:
                    call.stall_qid = None  # don't let the cancel hook re-clear
                cancelled = self.bus_call_cancel(call_id=call.id) if call else []
                self.inbox.mark_answered(q.id, text, answer_job_id=0, answered_by=op)
                self.inbox.publish("answered", q)
                return {"ok": True, "kind": "bus_stall", "cancelled": cancelled}
            # "wait": keep the call alive; suppress re-asking for one more
            # stall window. No prompt is queued.
            if call is not None:
                call.flagged_stalled = False
                call.stall_qid = None
                call.stall_snooze_until = time.time() + self._bus_stall_threshold()
            self.inbox.mark_answered(q.id, text, answer_job_id=0, answered_by=op)
            self.inbox.publish("answered", q)
            return {"ok": True, "kind": "bus_stall", "action": "waiting"}
        # Delegation gates: the asking agent is paused inside ask_agent waiting
        # on a future. Resolve the future with the operator's text and skip
        # the normal "answer becomes the agent's next prompt" path.
        if q.kind == "delegation":
            pending = self.inbox.pop_pending(q.id)
            if pending is None or pending.done():
                # The delegation timed out (or was cancelled) earlier and
                # removed the future; the asking agent has already returned
                # an error to its caller. Mark the question answered with
                # the operator's text so it doesn't stay in the inbox as
                # 'unanswered' forever — otherwise the operator can only
                # dismiss it via `questions clear`, not via the answer flow.
                self.inbox.mark_answered(q.id, text, answer_job_id=0, answered_by=op)
                self.inbox.publish("answered", q)
                return {"error": f"delegation #{qid} no longer awaiting reply (cleared)"}
            pending.set_result(text)
            self.inbox.mark_answered(q.id, text, answer_job_id=0, answered_by=op)
            self.inbox.publish("answered", q)
            # Provenance event: the answer flowed back to the asking
            # agent through the ask_agent future, not as a new prompt.
            # Log on the asking agent's transcript AND publish to the
            # live stream so the web pane shows the operator's reply.
            await runner._log_provenance(
                "operator_answer",
                text,
                source="operator",
                recipient=runner.name,
                qid=q.id,
                extras={"qid": q.id, "kind": "delegation", "answered_by": op},
            )
            return {"ok": True, "kind": "delegation"}
        wait = bool(req.get("wait", False))
        fut = asyncio.get_running_loop().create_future() if wait else None
        expanded, subs = self.expand_prompt(text)
        if subs:
            await _emit(
                runner.name,
                "system",
                f"answer: expanded {len(subs)} context ref(s)",
            )
        # Provenance event: log the operator's answer on the asking
        # agent's transcript BEFORE submit AND publish to the live
        # stream so the web pane shows it. The expanded answer ALSO
        # becomes the next prompt for the agent (regular question
        # case), but provenance tags it distinctly from user_message.
        await runner._log_provenance(
            "operator_answer",
            expanded,
            source="operator",
            recipient=runner.name,
            qid=q.id,
            extras={"qid": q.id, "kind": "operator", "answered_by": op},
        )
        job = runner.submit(expanded, future=fut, suppress_banner=wait)
        self.inbox.mark_answered(q.id, text, job.id, answered_by=op)
        self.inbox.publish("answered", q)
        if fut is not None:
            timeout = req.get("timeout") or runner.prompt_timeout + 30
            try:
                done = await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError:
                return {"task_id": job.id, "error": "wait timeout"}
            return {
                "task_id": done.id,
                "agent": q.agent,
                "result": done.result,
                "error": done.error,
                "duration": (done.finished_at or 0) - (done.started_at or 0),
            }
        return {"ok": True, "agent": q.agent, "task_id": job.id}
