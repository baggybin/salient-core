"""Pure helpers used across the daemon — no Daemon/AgentRunner dependencies.

Three groups live here:
  • Display tags (`_tag`, `_ok_dot`, `_warn_dot`, `_agent_chip`).
  • SDK CLI-death classification (`_is_cli_terminated_error`,
    `_extract_exit_code`, `_sdk_died_message`, `_quiet_asyncio_handler`).
  • Operator-marker / context-value plumbing (`_QUESTION_MARKER_RE`,
    `_CONTEXT_VAR_RE`, `_wrap_context_value`, `_extract_marker_questions`,
    `_strip_question_markers`).
  • Small shared dataclasses (`BusCall`, `Job`).

A few module-level constants (`DEFAULT_SOCKET`, `DEFAULT_PROMPT_TIMEOUT`,
`DEFAULT_IDLE_TIMEOUT`, `DEFAULT_TAIL_BUFFER`, `_TEXT_FULL_INLINE_CAP`) also
live here because every consumer wants them, and they have no class deps.
"""

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..display import _agent_color, _c

# CLIConnectionError — the SDK's public export (re-exported from its
# _errors module, but we take it from the top-level package so we don't
# couple to the private submodule path). Pulled in so we can detect it
# specifically at the _run_loop catch site and translate to a clean,
# actionable operator-facing message instead of leaking the full SDK
# traceback path into the agent log. The asyncio loop-level exception
# handler (_quiet_asyncio_handler, registered in amain) also references
# the class to silence redundant background-task tracebacks that the
# SDK's _handle_control_request fires when the CLI subprocess dies
# mid-write.
_CLIConnErr: type[BaseException] | None
try:
    from claude_agent_sdk import CLIConnectionError as _CLIConnErr
except ImportError:
    _CLIConnErr = None  # SDK without the public symbol; fall back to message-match


# ── Display tags ─────────────────────────────────────────────────────


def _tag(text: str) -> str:
    """Cyan dim '[daemon]' tag prefix used on every startup line."""
    return _c(f"[{text}]", "2;36")


def _ok_dot() -> str:
    return _c("●", "32")


def _warn_dot() -> str:
    return _c("●", "33")


def _agent_chip(name: str) -> str:
    return _c(name, _agent_color(name))


# ── Constants ────────────────────────────────────────────────────────

DEFAULT_SOCKET = Path("/tmp/salient.sock")
DEFAULT_PROMPT_TIMEOUT = 1200.0
DEFAULT_IDLE_TIMEOUT = 0.0  # 0 = never idle out

# Cap on inline-expand payload per published event. When _log_truncated
# attaches the full text to a truncated event (so the web console can
# render the `... [+N chars]` marker as a clickable expand button), the
# full text is shipped over the wire AND lives in the per-agent ring
# buffer until aged out. 64 KB × tail_buffer_size (~250) bounds worst-
# case memory to ~16 MB per agent — fine for a coordinator's terminal,
# noticeable but acceptable for an action agent with many large tool
# results. Beyond this cap, the marker stays non-clickable and the
# operator falls back to `salientctl logs grep` / read_evidence.
_TEXT_FULL_INLINE_CAP = 65536

DEFAULT_TAIL_BUFFER = 200  # ring-buffer size of recent events for tail replay
DEFAULT_HISTORY_MAX = 500  # cap on in-memory completed-Job history per runner
# (the full record persists to the jobs table; only
# the live list is bounded). Reads use the tail
# (history[-10:]) + a separate jobs_recorded counter.


# ── SDK CLI subprocess death — friendly translation ──────────────────
#
# The Claude Agent SDK drives a `claude` CLI subprocess for each agent.
# When that subprocess dies (operator kill, OOM, daemon shutdown, etc.)
# the SDK surfaces it via CLIConnectionError on the next write, plus
# stderr noise from a background `_handle_control_request` task. The
# raw error message is unactionable for the operator ("Cannot write to
# terminated process (exit code: 143)") — they need to know WHY (signal
# name) and WHAT TO DO (restart the agent). The helpers below classify
# the exit code and produce that message. Used by _run_loop's catch
# block and by the loop-level asyncio exception handler in amain.

_SIGNAL_NAMES_FOR_EXIT_CODE: dict[int, str] = {
    # bash + python conventions: 128 + N when terminated by signal N.
    # Limited to the ones we actually expect to see in this context;
    # exhaustive enumeration adds line count for no gain.
    143: "SIGTERM",  # operator kill / killswitch / daemon shutdown
    137: "SIGKILL",  # OOM-killer or forceful kill -9
    139: "SIGSEGV",  # CLI subprocess crashed
    134: "SIGABRT",  # CLI subprocess assertion / abort
    131: "SIGQUIT",
    130: "SIGINT",  # Ctrl+C in the daemon's terminal
}


def _is_cli_terminated_error(exc: BaseException) -> bool:
    """True when `exc` represents the SDK's CLI subprocess having
    terminated. We use the class when the SDK exposes it, plus a
    message-substring fallback so the test path doesn't depend on the
    private SDK module being importable."""
    if _CLIConnErr is not None and isinstance(exc, _CLIConnErr):
        return True
    text = str(exc)
    return "Cannot write to terminated process" in text or "Command failed with exit code" in text


def _extract_exit_code(exc: BaseException) -> int | None:
    """Parse the exit code out of an SDK error message. SDK formats:
       'Cannot write to terminated process (exit code: 143)'
       'Command failed with exit code 143 (exit code: 143)'
    Returns None if no parseable code."""
    m = re.search(r"exit code[:\s]+(\d+)", str(exc))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


# How many trailing stderr lines to fold into the operator-facing
# error message body. The full ring buffer (default 200 lines, set on
# AgentRunner.stderr_buffer) stays on the runner for JSONL logging
# and `salientctl logs grep`; this cap is just for the single
# tool-error event line. 10 keeps the event readable in the terminal
# pane while still showing the trailing CLI error frames (which is
# usually where the smoking gun lives).
_STDERR_TAIL_IN_MESSAGE = 10


def _sdk_died_message(
    agent_name: str,
    exc: BaseException | None,
    *,
    exit_code_override: int | None = None,
    stderr_tail: "Iterable[str] | None" = None,
) -> str:
    """Build the operator-facing one-liner for a CLI-died event.
    Names the signal when we recognize the exit code; always includes
    the restart command so the operator's next move is one click away.

    Args:
        exc: the SDK exception that surfaced the death. Optional —
            when the caller is classifying via returncode probe
            (subprocess died but the exception text wasn't one of our
            recognized patterns), pass ``None`` and use
            ``exit_code_override`` instead.
        exit_code_override: subprocess returncode read directly via
            ``_probe_proc_returncode``. More reliable than parsing
            the SDK error text — used as the secondary classifier when
            ``_is_cli_terminated_error`` says "no" but the proc is
            actually dead.
        stderr_tail: optional iterable of recent stderr lines from the
            SDK subprocess (typically the runner's stderr_buffer
            deque). When provided, the last ``_STDERR_TAIL_IN_MESSAGE``
            lines are folded into the message body. Critical for
            diagnosing deaths where the SDK swallowed the underlying
            CLI error (npm complaints, model rejections, etc.).
    """
    code = exit_code_override
    if code is None and exc is not None:
        code = _extract_exit_code(exc)
    if code is not None and code in _SIGNAL_NAMES_FOR_EXIT_CODE:
        sig = _SIGNAL_NAMES_FOR_EXIT_CODE[code]
        cause_hint = {
            "SIGTERM": "operator action (kill / killswitch), daemon shutdown, or OOM-killer",
            "SIGKILL": "OOM-killer or forceful kill -9 — check "
            "`journalctl -k | grep oom` for memory pressure",
            "SIGSEGV": "CLI subprocess crashed — likely a bug in the "
            "SDK / claude CLI; check daemon stderr",
            "SIGABRT": "CLI subprocess aborted — assertion failure or internal error",
            "SIGINT": "Ctrl+C in the daemon terminal",
        }.get(sig, "external signal")
        body = (
            f"agent {agent_name!r} CLI subprocess died "
            f"({sig}, exit={code}). Cause: {cause_hint}. "
            f"Restart with `salient start {agent_name}`."
        )
    elif code is not None:
        body = (
            f"agent {agent_name!r} CLI subprocess died "
            f"(exit={code}). Restart with `salient start {agent_name}`."
        )
    else:
        body = (
            f"agent {agent_name!r} CLI subprocess died unexpectedly. "
            f"Restart with `salient start {agent_name}`."
        )
    if stderr_tail is not None:
        tail = [ln for ln in list(stderr_tail) if ln]
        if tail:
            tail = tail[-_STDERR_TAIL_IN_MESSAGE:]
            body += "\n  stderr tail (last " + str(len(tail)) + "):\n    "
            body += "\n    ".join(tail)
    return body


def _probe_proc_returncode(client: Any) -> int | None:
    """Reach through the SDK client to the subprocess returncode.

    Used as the SECONDARY classification signal when the SDK's
    exception text didn't match one of the patterns
    ``_is_cli_terminated_error`` knows. The subprocess returncode is
    the direct, ground-truth signal of "the child is dead" — text
    heuristics can lose to SDK version churn (Anthropic rewords its
    errors occasionally), but the returncode is set by the kernel.

    Defensive throughout: walks private SDK attrs via getattr so a
    refactor inside claude-agent-sdk that moves ``_transport`` or
    ``_process`` doesn't crash the daemon's catch site — we just
    return None, and the caller falls back to the generic error path.
    """
    if client is None:
        return None
    transport = getattr(client, "_transport", None)
    if transport is None:
        return None
    proc = getattr(transport, "_process", None)
    if proc is None:
        return None
    return getattr(proc, "returncode", None)


def classify_run_loop_error(
    agent_name: str,
    exc: BaseException,
    client: Any,
    stderr_tail: "Iterable[str] | None" = None,
) -> str:
    """Decide whether an exception from the SDK's response loop is a
    subprocess-death (operator-actionable: friendly message + signal
    name + restart hint + stderr tail) or a genuine SDK internal
    error (raw type+message, traceback goes to JSONL for later
    triage). Three classification layers, in order:

      1. ``_is_cli_terminated_error`` — typed CLIConnectionError plus
         two recognized message-substring patterns. Cheapest path;
         catches the common kill/timeout cases.

      2. ``_probe_proc_returncode`` — direct kernel-set returncode on
         the SDK's underlying subprocess. Catches deaths where the
         SDK wrapped the failure in a class/message we don't
         recognize, but the child clearly exited.

      3. Fallthrough — the SDK raised an exception but the subprocess
         is still alive. Genuine internal SDK error. Surface raw
         type+message so the operator has the type name to grep.

    Extracted as a free function so the runner's main loop catch
    site is one line AND the classification logic is unit-testable
    without needing to spin up a real SDK / runner.
    """
    if _is_cli_terminated_error(exc):
        return _sdk_died_message(agent_name, exc, stderr_tail=stderr_tail)
    rc = _probe_proc_returncode(client)
    if rc is not None and rc != 0:
        return _sdk_died_message(
            agent_name,
            None,
            exit_code_override=rc,
            stderr_tail=stderr_tail,
        )
    return f"{type(exc).__name__}: {exc}"


def _quiet_asyncio_handler(loop, context):
    """Loop-level asyncio exception handler.

    Silences the redundant 'Task exception was never retrieved'
    traceback the SDK's `_handle_control_request` background task
    fires when the CLI subprocess dies mid-write. The same death is
    ALREADY surfaced cleanly via _run_loop's catch site (which logs
    `agent X CLI subprocess died (SIGTERM, exit=143). Restart with...`)
    — the background-task traceback is pure noise.

    Falls through to the default handler for anything we don't
    recognize, so genuine bugs still surface."""
    exc = context.get("exception")
    msg = context.get("message", "") or ""
    # Three patterns we want to swallow:
    #   1. CLIConnectionError class (SDK exposes it)
    #   2. The message substring (in case the SDK wraps differently)
    #   3. The 'Fatal error in message reader' lines printed via stderr
    if exc is not None and _is_cli_terminated_error(exc):
        return
    if (
        "Cannot write to terminated process" in msg
        or "Fatal error in message reader" in msg
        or "Command failed with exit code" in msg
    ):
        return
    loop.default_exception_handler(context)


# ── Operator markers + context-value plumbing ────────────────────────

# Structured marker the agent embeds inline to file a question reliably.
# Matches <ask_operator>...</ask_operator> across newlines, case-insensitive.
_QUESTION_MARKER_RE = re.compile(r"<ask_operator>(.*?)</ask_operator>", re.DOTALL | re.IGNORECASE)

# Operator-side placeholder for context-bus values.
#   {{agent}}        → expand <context_value agent=... key="latest">VALUE</context_value>
#   {{agent.key}}    → expand <context_value agent=... key="key">VALUE</context_value>
_CONTEXT_VAR_RE = re.compile(r"\{\{\s*([\w][\w-]*)(?:\.([\w][\w.-]*))?\s*\}\}")


def _wrap_context_value(agent: str, key: str, value: str | None) -> str:
    """Wrap a context-store value in the structured marker an agent will see."""
    if value is None:
        return f'<context_value agent="{agent}" key="{key}">(missing)</context_value>'
    return f'<context_value agent="{agent}" key="{key}">\n{value}\n</context_value>'


def _extract_marker_questions(text: str) -> list[str]:
    if not text:
        return []
    return [m.group(1).strip() for m in _QUESTION_MARKER_RE.finditer(text)]


def _strip_question_markers(text: str) -> str:
    return _QUESTION_MARKER_RE.sub("", text).strip()


# ── Swarm-registry persistence shape ─────────────────────────────────
#
# The swarm registry ({orchestrator: {source, members, composition?,
# ephemeral?}}) is persisted as JSON under the `swarms` meta key and
# read back at boot in two places (Daemon.__init__ hydration and the
# _peek_swarms startup helper). `normalize_swarms` is the SINGLE source
# of truth for validating a decoded blob into the canonical registry —
# both readers call it, so the shape contract can't drift between them.
#
# Forward-compat: writes wrap the registry in a versioned envelope
# ({"_v": SWARM_SCHEMA_VERSION, "swarms": {...}}); normalize_swarms
# accepts BOTH that envelope and the legacy bare-dict format that
# pre-versioning daemons wrote, so an in-place upgrade reads old blobs
# and a downgrade fails closed (the bare-dict validator drops the
# envelope's reserved keys and yields an empty registry — no crash).

SWARM_SCHEMA_VERSION = 1


def normalize_swarms(data: Any) -> dict[str, dict[str, Any]]:
    """Validate a decoded ``swarms`` blob into the canonical
    ``{orch: {"source": str, "members": [str], "composition"?:
    [{"source": str, "members": [str]}], "ephemeral"?: bool}}``
    registry. Garbage entries are dropped, never raised — callers wrap
    only the surrounding json.loads in try/except.

    Accepts both the versioned envelope written by current daemons
    (``{"_v": N, "swarms": {...}}``) and the legacy bare-dict format
    (the registry at the top level) so old persisted blobs still
    hydrate after an upgrade.
    """
    # Unwrap the versioned envelope when present; otherwise treat the
    # payload itself as the registry (legacy pre-versioning format). The
    # `_v` guard disambiguates — a legacy registry never carries it.
    if isinstance(data, dict) and "_v" in data and isinstance(data.get("swarms"), dict):
        registry = data["swarms"]
    else:
        registry = data
    if not isinstance(registry, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for orch, entry in registry.items():
        if not isinstance(orch, str) or not isinstance(entry, dict):
            continue
        src = entry.get("source")
        members = entry.get("members") or []
        if not isinstance(src, str) or not isinstance(members, list):
            continue
        members = [m for m in members if isinstance(m, str)]
        row: dict[str, Any] = {"source": src, "members": members}
        # composition is optional — entries persisted before mixed-swarm
        # support only carry source + members and still restore.
        raw_comp = entry.get("composition")
        if isinstance(raw_comp, list):
            clean_comp: list[dict[str, Any]] = []
            for group in raw_comp:
                if not isinstance(group, dict):
                    continue
                g_src = group.get("source")
                g_mem = group.get("members") or []
                if not isinstance(g_src, str) or not isinstance(g_mem, list):
                    continue
                clean_comp.append(
                    {
                        "source": g_src,
                        "members": [m for m in g_mem if isinstance(m, str)],
                    }
                )
            if clean_comp:
                row["composition"] = clean_comp
        if "ephemeral" in entry:
            row["ephemeral"] = bool(entry.get("ephemeral"))
        out[orch] = row
    return out


def first_running_sibling_shadow(
    runners: dict[str, Any],
    name: str,
    primary: str | None,
) -> str | None:
    """Name of a RUNNING shadow of ``primary`` other than ``name``, else None.

    A "shadow" is any agent whose cfg carries ``substitute_for: <primary>``.
    Used by the agent-start guards to enforce one-shadow-per-primary: two live
    shadows of one primary make bus substitute-routing — which is
    first-match-wins (``salient/bus/_delegation.py``) — ambiguous, silently
    stranding the loser. Swarm forks are exempt for free: they strip
    ``substitute_for`` at spawn (``_cmd_swarm_create``), so they never match.
    Stopped runners are ignored.
    """
    if not primary:
        return None
    for rname, r in runners.items():
        if rname == name or getattr(r, "status", None) in ("stopped",):
            continue
        if (getattr(r, "cfg", None) or {}).get("substitute_for") == primary:
            return rname
    return None


# ── Shared dataclasses ───────────────────────────────────────────────


@dataclass
class BusCall:
    """In-flight `ask_agent` from one agent to another. Lives only while the
    bus tool's await is outstanding — registered on entry, resolved on any
    return path. Lets the operator see what's blocked and surgically cancel
    a leaked call without killing the asking agent."""

    id: int
    caller: str
    target: str
    prompt_preview: str
    started_at: float
    # "awaiting_agent_start" | "awaiting_delegation_gate" | "awaiting_reply"
    # Added 2026-05-18 for ask_agents swarm: "awaiting_fanout" is the
    # state of the synthetic PARENT row that owns N child calls.
    # Redispatch governor (operator-input pauses, same family as
    # "awaiting_delegation_gate"): "awaiting_redispatch_gate" — a single
    # ask_agent paused on the consecutive-dispatch gate; "awaiting_fanout_gate"
    # — the swarm PARENT paused on the batched fan-out gate.
    state: str
    future: asyncio.Future = field(repr=False)
    cancelled: bool = False
    # Set True by `_bus_call_reaper` after it has filed an operator
    # question for this stalled call. Prevents the reaper from re-filing
    # the same question every 30s; the operator's reply (or an explicit
    # bus_call_resolve) is what clears it from the registry.
    flagged_stalled: bool = False
    # Id of the kind="bus_stall" question the reaper filed for this call,
    # so the answer handler can map an operator "cancel"/"wait" reply back
    # to THIS call, and so resolve/cancel can auto-clear a dangling stall
    # question when the call ends another way. None once cleared.
    stall_qid: int | None = None
    # When the operator answers "wait", the reaper suppresses re-flagging
    # until this wall-clock time (one more stall window), so "wait" isn't
    # immediately re-asked on the next 30s tick.
    stall_snooze_until: float = 0.0
    # Swarm/fan-out hierarchy (ask_agents, added 2026-05-18). For a
    # single ask_agent call: both None (root-level call, behaves as
    # before). For ask_agents children: parent_call_id points at the
    # synthetic swarm-parent BusCall. For the synthetic parent itself:
    # parent_call_id is None and swarm_role == "parent". The tree
    # renderer pivots on these to draw the hierarchy; bus_call_cancel
    # walks parent_call_id to cascade cancellation to children.
    parent_call_id: int | None = None
    swarm_role: str | None = None
    # Id of the child runner's Job, recorded at Phase 3 submit (set via
    # bus_call_set_child_job). Lets `bus_call_cancel` interrupt the child
    # runner (cancel_job) instead of only settling the caller's future — which
    # would leave the child burning tokens until its own timeout. None until
    # the call actually dispatches (it never reaches Phase 3 if a gate denies).
    child_job_id: int | None = None
    # Delegation depth, inferred at register from the in-flight call that
    # spawned the caller (target == caller): root bus call = 0, each level
    # down +1. Powers the MAX_DELEGATION_DEPTH admission cap so a runaway
    # ladder/fan-out can't nest unboundedly. 0 for swarm-parent labels.
    depth: int = 0


@dataclass
class Job:
    id: int
    prompt: str
    submitted_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: str = ""
    error: str | None = None
    future: asyncio.Future | None = None
    tool_question_ids: list[int] = field(default_factory=list)
    # True when the operator initiated this with `--wait` and is already
    # going to print the reply themselves — skip the REPL reply banner so
    # they don't see it twice. NOT set when another agent attached the
    # future via `ask_agent`; those replies are normal agent activity the
    # operator should see in the REPL.
    suppress_banner: bool = False
    # Per-dispatch turn budget threaded down from bus._render_delegation_envelope.
    # The runner uses this as a HARD wire-level ceiling: if AssistantMessage
    # count exceeds max_turns_hint + buffer, the loop breaks and a synthetic
    # PARTIAL completion resolves the BusCall future. Without this, the SOFT
    # ceiling in the envelope's prose is ignorable and shadows have been
    # observed running to the SDK's internal ~31-turn cap, dangling caller
    # futures until the 1200s timeout. None ⇒ no cap (operator-initiated
    # prompts, full-budget conversations).
    max_turns_hint: int | None = None
    # Inert in the kernel: marks a job as a verification/replay leg. The kernel
    # only stores + forwards it (never branches on it); a downstream
    # verification subsystem consumes it. Kept here so the daemon can cut over
    # independently of that subsystem. See salient_core.daemon.runner.submit.
    verification_leg: bool = False
