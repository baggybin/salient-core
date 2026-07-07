"""Shared helpers for the bus package.

Extracted from salient/bus.py during the package split.

These constants and functions are referenced by closures defined in
multiple per-group modules (_context.py, _delegation.py, etc.). Per
the post-mortem on the tools.py monolith split
(feedback_monolith_split_constant_leak.md): leading-underscore names
do NOT transit `from x import *` unless x.__all__ lists them. So
EVERY underscore name defined here is added to __all__ below — that's
what lets per-group files do `from ._common import *` and find them.
"""

from __future__ import annotations

import json
import logging
import re as _re
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, overload, runtime_checkable

from claude_agent_sdk import tool
from pydantic import BaseModel, ValidationError

from ._flags import _NO_FLAGS, BusFlags

if TYPE_CHECKING:
    from ..coord.questions import Question
    from ..protocols import DaemonServices

_log = logging.getLogger("salient.bus")


# ── Skin-module registry ─────────────────────────────────────────────
# A few bus tools lazily reach into downstream SKIN modules the kernel does not
# ship (MITRE technique attribution, the skill-playbook search, evidence-blob
# truncation). Rather than a hard import per site (which ModuleNotFound-crashes
# standalone), a downstream registers the modules by name via
# ``set_bus_skin_modules(techniques=..., skills=..., truncate=...)`` at startup;
# the tool resolves them at call time through ``_skin_module``. A tool whose
# skin module is unregistered raises a clear error (caught + surfaced by the
# handler) instead of an ImportError. Same call-time injection idiom as the
# other bus seams.
_bus_skin_modules: dict[str, Any] = {}


def set_bus_skin_modules(**modules: Any) -> None:
    """Register downstream skin modules the bus tools reach into (by keyword —
    e.g. ``techniques=...``, ``skills=...``, ``truncate=...``). A value may be
    the module itself OR a zero-arg callable returning it (a thunk), which
    preserves the tools' lazy-import semantics — a heavy skin module isn't
    pulled until its tool first fires. None values are ignored. Called once at
    startup by a skin."""
    _bus_skin_modules.update({k: v for k, v in modules.items() if v is not None})


def _skin_module(name: str) -> Any:
    """The registered skin module ``name`` (raises if a skin never registered
    it — a tool that needs it can't run without the downstream). A registered
    thunk is resolved on first use and cached."""
    mod = _bus_skin_modules.get(name)
    if mod is None:
        raise RuntimeError(
            f"bus skin module {name!r} is not registered — the downstream must "
            f"call salient_core.bus.set_bus_skin_modules({name}=...) at startup"
        )
    if callable(mod):  # a thunk (a module object is not callable) — resolve + cache
        mod = mod()
        _bus_skin_modules[name] = mod
    return mod


def _text(s: str, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": s}]}
    if error:
        out["is_error"] = True
    return out


# ─── bus_tool: schema-from-model + runtime validation ──────────────────────


def _clean_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Turn a Pydantic ``model_json_schema()`` into a clean tool input_schema:
    inline ``$defs`` / ``$ref`` (a model reads an inlined object better than a
    ``$ref``) and drop cosmetic ``title`` keys (root AND per-property) plus the
    ``default`` keyword. Leaves ``enum`` / ``minimum`` / ``minItems`` etc. intact.

    ``default`` is stripped deliberately: an optional field REQUIRES a Pydantic
    default (that's what drops it from ``required``), but the default is a
    server-side fill, not part of the wire contract the model must see. Dropping
    it keeps the advertised schema minimal and uniform — no golden ever carries a
    ``default`` — and lets a ``field: T = <val>`` optional reproduce the bare
    ``{"type": ...}`` shape the pre-migration inline/shorthand schemas advertised.
    Field-named ``default``/``title`` survive: only the schema KEYWORD is dropped
    (see the ``properties`` special-case below).

    Bus-tool models must be flat/acyclic: a ``$ref`` cycle (a self-referential
    model) raises rather than looping forever, and any ``$ref``/``$defs`` that
    survives inlining (an un-handled schema shape) raises rather than shipping a
    broken advertised schema."""
    defs = schema.get("$defs", {})

    def _resolve(node: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = str(node["$ref"]).split("/")[-1]
                if ref in stack:
                    raise ValueError(
                        f"bus_tool: recursive model schema (`$ref` cycle on {ref!r}) "
                        "is not supported — bus-tool models must be flat/acyclic."
                    )
                target = _resolve(dict(defs.get(ref, {})), stack + (ref,))
                siblings = {
                    k: _resolve(v, stack) for k, v in node.items() if k not in ("$ref", "title")
                }
                return {**target, **siblings}
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in ("$defs", "title", "default"):
                    continue
                # `properties` keys are field NAMES (data), not schema keywords:
                # preserve them verbatim and only clean their schema values —
                # else a field literally named "title"/"$defs" would be dropped.
                if k == "properties" and isinstance(v, dict):
                    out[k] = {pname: _resolve(pv, stack) for pname, pv in v.items()}
                else:
                    out[k] = _resolve(v, stack)
            return out
        if isinstance(node, list):
            return [_resolve(x, stack) for x in node]
        return node

    cleaned = cast("dict[str, Any]", _resolve(schema))
    leftover = json.dumps(cleaned)
    if "$ref" in leftover or "$defs" in leftover:
        raise ValueError(
            "bus_tool: schema still contains $ref/$defs after cleaning — "
            "unsupported (recursive or non-inlinable) model schema."
        )
    return cleaned


def _format_bus_validation_error(tool_name: str, exc: ValidationError) -> str:
    """Turn a Pydantic ValidationError into an agent-actionable one-liner."""
    parts = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", ())) or "(args)"
        parts.append(f"{loc}: {e.get('msg')}")
    return (
        f"invalid arguments for {tool_name}: "
        + "; ".join(parts)
        + " — fix the argument types / required fields and call again."
    )


_Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
_RoutedHandler = Callable[[dict[str, Any], BusFlags], Awaitable[dict[str, Any]]]


@runtime_checkable
class BusTool(Protocol):
    """Structural type of a plain ``@bus_tool``: the model-facing ``.handler``
    (SDK wire path) and the in-process ``.trusted`` entry both take ONLY a
    declared-args dict — no routing flags."""

    name: str
    input_schema: dict[str, Any]

    def handler(self, args: dict[str, Any]) -> Awaitable[dict[str, Any]]: ...
    def trusted(self, args: dict[str, Any]) -> Awaitable[dict[str, Any]]: ...


@runtime_checkable
class RoutedBusTool(Protocol):
    """Structural type of a ``@bus_tool(routed=True)`` tool: ``.trusted`` REQUIRES
    a typed ``flags`` kwarg — the routing channel is visible in the type, not
    smuggled through the payload."""

    name: str
    input_schema: dict[str, Any]

    def handler(self, args: dict[str, Any]) -> Awaitable[dict[str, Any]]: ...
    def trusted(self, args: dict[str, Any], *, flags: BusFlags) -> Awaitable[dict[str, Any]]: ...


@overload
def bus_tool(
    name: str, description: str, model: type[BaseModel], *, routed: Literal[False] = ...
) -> Callable[[_Handler], BusTool]: ...
@overload
def bus_tool(
    name: str, description: str, model: type[BaseModel], *, routed: Literal[True]
) -> Callable[[_RoutedHandler], RoutedBusTool]: ...


def bus_tool(name: str, description: str, model: type[BaseModel], *, routed: bool = False) -> Any:
    """``@tool``'s validating cousin: the input schema is DERIVED from ``model``
    and incoming args are validated + coerced against it before the handler
    runs. On a schema mismatch the agent gets a structured tool-error (which it
    can self-correct) instead of a silent coercion or a mid-handler crash.
    Single source of truth for BOTH the advertised schema and runtime validation.

    Two entry points share one pipeline:

    * ``.handler`` — the SDK/wire path. The handler receives ONLY declared,
      validated fields; a routed handler always gets the default ``BusFlags()``.
      A wire-injected ``_``-key is dropped at validation (``extra='ignore'``), so
      a model can never reach the handler with a trusted routing flag.
    * ``.trusted`` — in-process ONLY. For ``routed=True``, internal callers pass
      routing via the typed ``flags`` kwarg. Privilege is *which callable you
      hold*, not what data you send.

    The model relies on ``extra='ignore'`` (Pydantic's default) so undeclared
    keys drop at validation; ``additionalProperties: false`` is stamped on the
    ADVERTISED schema as defense-in-depth (advisory — the drop is the real
    enforcement)."""
    # The wire path's security rests on undeclared keys (incl. reserved `_`-keys)
    # being DROPPED at validation, not raised. A model with extra='forbid' would
    # turn a wire-injected `_`-key into a model-visible ValidationError — handing
    # a probe exactly the feedback the step-1 wire-strip fix denies it. Reject at
    # registration (fails loud at import) rather than let it regress silently.
    if model.model_config.get("extra") == "forbid":
        raise ValueError(
            f"bus_tool {name}: model {model.__name__} sets extra='forbid'; the wire "
            "path relies on undeclared keys being dropped at validation, not raised "
            "— leave `extra` at its default ('ignore')."
        )
    schema = _clean_tool_schema(model.model_json_schema())
    schema["additionalProperties"] = False

    def decorator(handler: Any) -> Any:
        async def _invoke(raw: dict[str, Any], *, trusted: bool, flags: BusFlags) -> dict[str, Any]:
            if not trusted:
                smuggled = [k for k in raw if isinstance(k, str) and k.startswith("_")]
                if smuggled:
                    _log.warning(
                        "bus_tool %s: wire args carried reserved routing keys %s — "
                        "ignored (models cannot set routing flags).",
                        name,
                        smuggled,
                    )
            elif routed:
                stray = [k for k in raw if isinstance(k, str) and k.startswith("_")]
                if stray:
                    raise ValueError(
                        f"bus_tool {name}: trusted routed call received reserved routing "
                        f"keys {stray} in args — pass them via flags=BusFlags(...) instead."
                    )
            try:
                validated = model.model_validate(raw)
            except ValidationError as exc:
                return _text(_format_bus_validation_error(name, exc), error=True)
            clean = validated.model_dump()
            if routed:
                return cast("dict[str, Any]", await handler(clean, flags))
            return cast("dict[str, Any]", await handler(clean))

        @tool(name, description, schema)
        async def _validated(args: dict[str, Any]) -> dict[str, Any]:
            return await _invoke(
                args if isinstance(args, dict) else {}, trusted=False, flags=_NO_FLAGS
            )

        if routed:

            async def _trusted(
                args: dict[str, Any], *, flags: BusFlags = _NO_FLAGS
            ) -> dict[str, Any]:
                return await _invoke(
                    dict(args) if isinstance(args, dict) else {}, trusted=True, flags=flags
                )
        else:

            async def _trusted(  # type: ignore[misc]
                args: dict[str, Any],
            ) -> dict[str, Any]:
                return await _invoke(
                    dict(args) if isinstance(args, dict) else {}, trusted=True, flags=_NO_FLAGS
                )

        _validated.trusted = _trusted
        return cast("RoutedBusTool | BusTool", _validated)

    return decorator


def _unwrap(reply: dict[str, Any]) -> tuple[bool, str]:
    """(ok, text) from an ask_agent `_text` envelope.

    Reads BOTH error-flag spellings: `_text(error=True)` writes snake_case
    `is_error` (above); the MCP transport convention is camelCase `isError`.
    Reading both keeps callers correct whether the reply came straight off
    `ask_agent.handler(...)` (always snake_case) or through an SDK-wrapped
    path. Single-sourced here so ask_agents._run_child and the consensus
    panel can't drift apart on the key name (they did once — see N6).
    """
    err = bool(reply.get("isError") or reply.get("is_error"))
    text = ""
    for block in reply.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            break
    return (not err), text


from ._context_store import _SCHEMA, ContextStore  # noqa: F401

# Credential kinds accepted by cred_record now live in the credential-vocabulary
# seam (salient_core.memory.credentials): generic kernel defaults + whatever a
# downstream skin registers. Consumers call `cred_kinds()` at runtime.


def _delegation_gated(policy_value: Any, target: str) -> bool:
    """Decide if `ask_agent → target` should be operator-gated.

    Accepts:
      - falsy / missing       → not gated
      - True / "*" / ["*"]    → gate every delegation
      - list[str]             → gate when target is in the list
    """
    if not policy_value:
        return False
    if policy_value is True:
        return True
    if isinstance(policy_value, str):
        return policy_value.strip() in ("*", target)
    if isinstance(policy_value, (list, tuple, set)):
        items = {str(x).strip() for x in policy_value}
        return "*" in items or target in items
    return False


def _trust_covers(bus_trusted: Any, target: str) -> bool:
    """Decide if a `bus_trusted` caller is trusted FOR `target` — i.e. may
    bypass the agent-start + delegation approval gates for this delegation.

    Accepts (mirrors `_delegation_gated`'s value shapes, inverted meaning):
      - True / "*" / ["*"]    → trust EVERY target (back-compat)
      - list[str]             → trust only when target is in the list
      - falsy / missing       → not trusted (gate fires normally)
    """
    if bus_trusted is True:
        return True
    if isinstance(bus_trusted, str):
        return bus_trusted.strip() in ("*", target)
    if isinstance(bus_trusted, (list, tuple, set)):
        items = {str(x).strip() for x in bus_trusted}
        return "*" in items or target in items
    return False


# Per-turn estimate for caller-side timeout sizing. Empirical upper
# bound for tool-bearing reasoning agents (disasm, multi-step web
# probes, staging): each turn ≈ 30-60s of wall time. Use 60 for
# safety so the bus doesn't time out the caller while the target is
# still legitimately working.
_PER_TURN_SECS = 60
# Hard cap on the computed timeout. 4 hours is long enough for a
# 12-member swarm at max_turns=50 with deep workloads (~3840s of
# expected wall) plus generous slop, but bounded so a runaway agent
# can't leak a coroutine forever. Beyond this, the operator should
# bus_cancel manually or the reaper will eventually flag it stalled.
_TIMEOUT_HARD_CAP_SECS = 14400


def _compute_ask_agent_timeout(
    *,
    daemon: Any,
    target_name: str,
    target_runner: Any,
    max_turns_hint: int | None,
) -> float | None:
    """Caller-side wait window for ``ask_agent`` → ``target_name``.

    Returns ``None`` when the target has no prompt_timeout configured
    (asyncio.wait_for treats None as "wait forever") and neither
    other signal contributes — same shape as the pre-fix behaviour
    for that edge case.

    Composition (take MAX of the three signals, then clamp to hard cap):

      (a) base = ``target_runner.prompt_timeout + 60`` if positive
      (b) hint = ``max_turns_hint * 60 + 60`` if positive
      (c) swarm = composition-derived estimate when the target is a
          ``swarm_orchestrator: true`` agent. Looks up the swarm's
          composition + each source's ``swarm_member_max_turns``
          floor; estimates wall as ``max_floor * 60`` (slowest child)
          + ``max(20, members) * 60`` (orchestrator's own
          decomposition/synthesis turns) + 120s slop.

    This is the function the ask_agent path calls; isolated so the
    test surface can pin each branch independently and so future
    overrides (e.g., per-agent timeout multiplier) land in one place.
    """
    base = 0
    target_prompt_timeout = getattr(target_runner, "prompt_timeout", 0) or 0
    if target_prompt_timeout > 0:
        base = int(target_prompt_timeout) + 60

    hint = 0
    if isinstance(max_turns_hint, int) and max_turns_hint > 0:
        hint = max_turns_hint * _PER_TURN_SECS + 60

    swarm = 0
    all_cfgs = getattr(daemon, "all_cfgs", {}) or {}
    target_cfg = all_cfgs.get(target_name) or {}
    if not target_cfg.get("swarm_orchestrator"):
        # Some swarm orchestrators are synthesized at runtime (not in
        # all_cfgs); check the runner's cfg too.
        target_cfg = getattr(target_runner, "cfg", None) or target_cfg
    if target_cfg.get("swarm_orchestrator"):
        swarms = getattr(daemon, "_swarms", {}) or {}
        entry = swarms.get(target_name) or {}
        members = list(entry.get("members") or [])
        composition = list(entry.get("composition") or [])
        max_floor = 0
        for group in composition:
            src = group.get("source")
            src_cfg = all_cfgs.get(src) or {}
            floor = src_cfg.get("swarm_member_max_turns")
            try:
                if floor is not None and int(floor) > max_floor:
                    max_floor = int(floor)
            except (TypeError, ValueError):
                pass
        if max_floor <= 0:
            # No source declared a floor — use a conservative default
            # matching the SDK's typical inner cap so the orch's child
            # wait is sized for "ordinary" workloads, not deep ones.
            max_floor = 30
        # Wall-time model: slowest child's wait dominates ask_agents
        # because it's concurrent. Add orchestrator's own budget (one
        # turn per member for decomposition + a few for synthesis,
        # floored at 20 turns to handle small swarms).
        child_wall = max_floor * _PER_TURN_SECS
        orch_budget = max(20, len(members)) * _PER_TURN_SECS
        swarm = child_wall + orch_budget + 120

    timeout = max(base, hint, swarm)
    if timeout <= 0:
        return None
    if timeout > _TIMEOUT_HARD_CAP_SECS:
        timeout = _TIMEOUT_HARD_CAP_SECS
    return float(timeout)


_APPROVE_WORDS = {
    "y",
    "yes",
    "yeah",
    "yep",
    "yup",
    "ok",
    "okay",
    "approve",
    "approved",
    "approves",
    "sure",
}
_DENY_WORDS = {
    "n",
    "no",
    "nope",
    "nah",
    "deny",
    "denied",
    "stop",
    "cancel",
    "skip",
    "abort",
}
# Back-off / qualifier words. When an approval word is FOLLOWED by one of
# these, the operator is approving-with-a-caveat ("okay but not to agent-x, go
# to bash instead") — which is NOT a blanket yes. We can't safely guess the
# caveat, so a qualified approval becomes a deny-with-reason: the caller
# turns that into an error the agent re-surfaces to the operator for an
# explicit answer. (Contractions like don't/can't are also caught via the
# `n't` suffix check below.)
_QUALIFIER_WORDS = {
    "but",
    "except",
    "unless",
    "however",
    "though",
    "although",
    "not",
    "no",
    "never",
    "instead",
    "avoid",
    "don't",
    "dont",
    "can't",
    "cant",
    "won't",
    "wont",
    "isn't",
    "isnt",
}


def _parse_yes_n(text: str) -> int:
    """Extract N from a 'yes N' bounded-credit reply (e.g. 'yes 5',
    'ok 3 please'). Returns 0 when the reply isn't an approval-with-count.

    Used ONLY by the redispatch gate. The shared `_parse_delegation_answer`
    reads just the first token (so 'yes 5' parses as a plain approve and the
    5 is dropped) AND it backs the Phase-1/Phase-2/subagent/lesson gates, so
    the count must be extracted here rather than by widening that parser.
    """
    raw = (text or "").strip()
    if not raw:
        return 0
    tokens = raw.lower().split()
    if not tokens:
        return 0
    first = tokens[0].rstrip(",.:;!?")
    if first not in _APPROVE_WORDS:
        return 0
    for tok in tokens[1:]:
        t = tok.strip(",.:;!?\"'")
        if t.isdigit():
            n = int(t)
            return n if n > 0 else 0
    return 0


def _parse_delegation_answer(text: str) -> tuple[str, str]:
    """Map the operator's free-text reply to (verdict, payload).

    verdict ∈ {"approve", "deny", "edit"}; payload is the reason (deny) or
    the rewritten prompt (edit). Defaults to deny so silence/garbage cannot
    accidentally approve a delegation.

    Accepts natural-language replies — looks at the FIRST word (after
    stripping trailing punctuation) so phrasings like "yes go for it",
    "no, that's the wrong target", or "ok, please do" all parse
    correctly. The operator shouldn't need a constrained vocabulary
    just to approve a delegation.
    """
    raw = (text or "").strip()
    if not raw:
        return "deny", "empty answer"
    low = raw.lower()

    # Edit takes precedence — explicit prefix carries a rewritten prompt.
    if low.startswith(("edit:", "edit ")):
        rest = raw.split(None, 1)[1] if low.startswith("edit ") else raw[len("edit:") :]
        return "edit", rest.strip()

    # First-word match: split off the leading token, strip trailing punct.
    tokens = low.split()
    first = tokens[0].rstrip(",.:;!?")
    if first in _APPROVE_WORDS:
        # Approval-with-a-caveat guard: a qualifier anywhere after the
        # leading "yes/ok/sure" means it isn't a blanket approval. Deny so
        # the agent re-asks the operator rather than acting on a partial
        # green-light. Plain approvals ("yes go ahead", "ok please do")
        # carry no qualifier and still approve.
        for tok in tokens[1:]:
            t = tok.strip(",.:;!?\"'")
            if t in _QUALIFIER_WORDS or t.endswith("n't"):
                return "deny", (
                    f"approval was qualified ({t!r} in {raw!r}); re-asking "
                    f"for an explicit yes or no with no caveats."
                )
        return "approve", ""
    if first in _DENY_WORDS:
        # Carry whatever followed as the reason. Strip leading separator
        # punctuation so "no, target wrong" → "target wrong" not ", target wrong".
        parts = raw.split(maxsplit=1)
        reason = parts[1].lstrip(",.:;!? \t") if len(parts) > 1 else ""
        return "deny", reason

    # First word didn't match either set — preserve the safe-by-default
    # behavior: ambiguous text is a deny with the raw payload as the
    # reason, so the agent can surface it back to the operator.
    return "deny", f"unrecognized answer: {raw!r}"


# Secret-shaped tokens to strip from delegation prose before it crosses the
# bus. Conservative + anchored to avoid eating ordinary text; this is
# defense-in-depth (delegation prose only), so a missed exotic key is worse
# than an occasional over-redaction. (label, compiled_pattern, replacement)
# — the label feeds the value-FREE redaction log; the matched secret is
# never recorded.
_SECRET_PATTERNS: list[tuple[str, Any, str]] = [
    ("openai-style-key", _re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "<redacted-key>"),
    ("github-token", _re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "<redacted-token>"),
    ("slack-token", _re.compile(r"\bxox[bpras]-[A-Za-z0-9-]{10,}\b"), "<redacted-token>"),
    ("aws-access-key-id", _re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-key>"),
    (
        "env-secret-assignment",
        _re.compile(r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PAT))\s*=\s*\S+"),
        r"\1=<redacted>",
    ),
]


def _redact_operator_infra(
    prompt: str,
    daemon: Any,
) -> tuple[str, list[str]]:
    """Strip operator-side infrastructure values from a delegation prompt
       before forwarding to the receiver agent.

    `recipe_discipline.md` already constrains what agents *write* into
    context entries / evidence. This is the same idea applied to what
    they *forward* over the bus. Operator-infra values become
    `<lhost>` / `<lport>` placeholders; the receiver looks the real
    values back up from the engagement profile when it's about to
    invoke a tool, where the substitution happens inside tool-call
    JSON rather than in forwarded prose.

    Receiver responsibility (not enforced here): the receiver MUST
    substitute the real values from the engagement profile's
    `network.lhost` / `network.lport` block before issuing a tool
    call. If a receiver copy-pastes a `<lhost>` placeholder into a
    destination-typed tool field (e.g. `tool(destination="<lhost>")`), the
    scope extractor will refuse fail-closed with an "unrecognized
    destination" error — clean failure, no security issue, but a confusing
    operator-side log message. For raw-argv tools like `bash.run`, the
    placeholder produces no extracted targets and the call is allowed
    through to fail at runtime ("host not found"). Regression tests
    in tests/test_redaction_placeholders.py pin both behaviors.

    Target IPs are NOT redacted — those are in-bounds, the receiver
    legitimately needs them in the prompt to know what to act against.
    127.0.0.1 / ::1 are excluded from the redaction set since they're
    documentation defaults, not engagement infra.

    Returns (redacted_prompt, list_of_substitutions_made_for_logging).
    """
    if not prompt:
        return prompt, []
    profile = (daemon.profile or {}) if hasattr(daemon, "profile") else {}
    network = profile.get("network") or {}
    redactions: list[str] = []
    out = prompt

    # LHOST + every IP that's currently bound to a local NIC. The
    # local-NIC set covers the case where the operator runs the daemon
    # on a multi-homed host and a callback uses a different interface
    # than the configured `network.lhost`.
    candidates: set[str] = set()
    lhost = network.get("lhost")
    if lhost:
        candidates.add(str(lhost))
    try:
        from ..policy.scope import _local_addresses

        for addr in _local_addresses():
            candidates.add(str(addr))
    except Exception:  # noqa: BLE001 — best-effort
        pass
    candidates.discard("127.0.0.1")
    candidates.discard("::1")

    import re

    for ip in candidates:
        if not ip or ip not in out:
            continue
        pattern = r"\b" + re.escape(ip) + r"\b"
        new_out, n = re.subn(pattern, "<lhost>", out)
        if n > 0:
            redactions.append(f"{ip} → <lhost> (×{n})")
            out = new_out

    # LPORT — only redact when network.lport is set, and only with
    # word boundaries so a port number doesn't eat an unrelated
    # 4-5 digit number elsewhere in the prompt.
    lport = network.get("lport")
    if lport:
        pattern = r"\b" + re.escape(str(lport)) + r"\b"
        new_out, n = re.subn(pattern, "<lport>", out)
        if n > 0:
            redactions.append(f"{lport} → <lport> (×{n})")
            out = new_out

    # Secret-shaped tokens (API keys, PATs, secret-named env assignments).
    # Log only the pattern LABEL + count — never the matched secret, so the
    # value can't leak into the operator log we just built to track leaks.
    for label, pat, repl in _SECRET_PATTERNS:
        new_out, n = pat.subn(repl, out)
        if n > 0:
            redactions.append(f"{label} redacted (×{n})")
            out = new_out

    return out, redactions


# Tool-call argument field names whose VALUES are credentials. Their values are
# replaced before a tool-call event is written to the JSONL log / events table /
# evidence, so captured secrets don't sit in plaintext on disk. Same generous
# philosophy as _SECRET_PATTERNS: a missed field is worse than the occasional
# over-redaction. NOTE: `ccache_path` is deliberately ABSENT — it's a filesystem
# path to a ticket cache, not the secret itself, and is load-bearing for debugging.
_SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pass",
        "pwd",
        "ssh_key",
        "private_key",
        "privkey",
        "api_token",
        "apitoken",
        "auth_token",
        "bearer_token",
        "secret",
    }
)
# Generic names that hold a secret ONLY inside a credential tool
# (cred_record(value=...), cred_search). Outside those, `value` / `hash` /
# `token` are ordinary fields and must NOT be redacted — doing so would gut
# unrelated log content. A downstream skin extends these sets for its own
# credential tools' field vocabulary.
_SECRET_FIELD_NAMES_CRED_ONLY = frozenset({"value", "hash", "token", "secret_value"})
_CRED_TOOL_MARKERS = ("cred_record", "cred_search")

_REDACTED_SECRET = "<redacted-secret>"

# Domain field names a downstream skin adds to the redaction set (lowercased).
# Mutable + separate from the frozen generic base so registration is additive and
# the kernel ships no domain vocabulary of its own — the same seam idiom as
# register_credential_vocab.
_EXTRA_SECRET_FIELD_NAMES: set[str] = set()


def register_secret_fields(names: Iterable[str]) -> None:
    """Register additional dict field NAMES whose values are redacted from logs,
    events, and evidence. The kernel ships a generic set (password / ssh_key /
    api_token / …); a downstream skin adds its domain credential field vocabulary
    here at startup. Call-time registration; read on every redaction pass."""
    _EXTRA_SECRET_FIELD_NAMES.update(n.lower() for n in names)


# Credential-tool name markers a downstream skin registers (lowercased). When a
# tool's name contains a marker, _redact_secret_fields also redacts the generic
# credential keys (value / hash / token) in its content. Mutable + separate so
# the kernel ships only its own generic tool names.
_EXTRA_CRED_TOOL_MARKERS: list[str] = []


def register_cred_tool_markers(markers: Iterable[str]) -> None:
    """Register additional credential-tool name markers. When a tool's name
    contains a marker, _redact_secret_fields widens redaction to the generic
    credential keys (value / hash / token) in that tool's content — the same
    call-time registration idiom as register_secret_fields."""
    _EXTRA_CRED_TOOL_MARKERS.extend(m.lower() for m in markers)


def _redact_secret_fields(content: Any, *, tool: str | None = None) -> Any:
    """Return a copy of ``content`` with secret-named dict-field VALUES replaced
    by ``<redacted-secret>``.

    Structural only — keys are matched by name; free text is NOT regex-scrubbed
    (intentional: result-text scrubbing is imperfect and would mangle tool
    output — persisted files are chmod 0600 instead). Recurses through nested
    dicts/lists so a secret under ``input`` (the tool-call arg dict) is caught.

    ``tool`` (the tool name carrying this content, if known) widens redaction to
    the generic credential keys (``value`` / ``hash`` / ``token``) for the cred
    pool tools only, where those keys hold the secret itself.
    """
    cred = tool is not None and any(
        m in tool.lower() for m in (*_CRED_TOOL_MARKERS, *_EXTRA_CRED_TOOL_MARKERS)
    )

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                key = k.lower() if isinstance(k, str) else k
                if (
                    key in _SECRET_FIELD_NAMES
                    or key in _EXTRA_SECRET_FIELD_NAMES
                    or (cred and key in _SECRET_FIELD_NAMES_CRED_ONLY)
                ):
                    out[k] = _REDACTED_SECRET
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(obj, list):
            return [_walk(x) for x in obj]
        if isinstance(obj, tuple):
            return tuple(_walk(x) for x in obj)
        return obj

    return _walk(content)


def _render_delegation_envelope(
    *,
    caller: str,
    max_turns_hint: int,
    deliverable: str,
) -> str:
    """Build the 'Delegation envelope' block prepended to a delegated
    prompt. Returns "" when neither budget nor deliverable was set, so
    callers that don't use the envelope see no change in behavior.

    The envelope is a soft contract: the child agent reads it in its
    first turn input. We CAN'T hard-cap turns at the SDK level for one
    call (max_turns is per-runner at construction); the budget here is
    a model-honored hint. The deliverable is the higher-leverage knob —
    naming a concrete output shape consistently shortens turns more than
    naming a number of turns does.
    """
    lines: list[str] = []
    if max_turns_hint and max_turns_hint > 0:
        # Imperative framing. The SDK doesn't hard-cap so the contract
        # is the model's word — make the contract explicit. The original
        # "should fit in at most N turns" was read as a target by
        # DeepSeek shadows (observed: 31-turn chains under max_turns=15
        # hints). New wording: HARD CEILING, return-what-you-have on
        # reach. See agent_protocol.md "Receiving a delegation envelope".
        lines.append(
            f"- Budget: HARD CEILING of {max_turns_hint} "
            f"turn{'s' if max_turns_hint != 1 else ''}. When you hit "
            f"that count, STOP whether the task is done or not — "
            f"return what you have plus a one-line note on what's "
            f"still missing. The caller will dispatch the next step "
            f"if needed. Treating this as a target instead of a "
            f"ceiling is the failure mode this envelope prevents."
        )
    if deliverable:
        lines.append(
            f"- Return: {deliverable}. That's the only output {caller!r} "
            f"needs back; everything else is incidental."
        )
    if not lines:
        return ""
    return "Delegation envelope from " + repr(caller) + ":\n" + "\n".join(lines)


def _format_swarm_payload(
    parent_call_id: int,
    aggregate: str,
    results: list[dict],
    cancelled_siblings: list[str] | None = None,
    warnings: list[str] | None = None,
) -> str:
    """Render the ask_agents result as a JSON string the model can parse.

    Shape:
      { ok: true|false, aggregate, parent_call_id, results: [...],
        cancelled_siblings?: [...], warnings?: [...] }

    For aggregate=all: ok is True (the swarm RAN successfully even if
    individual children failed; per-child ok flags carry success). For
    aggregate=any/race: ok mirrors the winner's ok — if all children
    failed in 'any' mode, we still return ok=False so the caller knows
    there's no usable result.

    `warnings` carries soft, non-failing advisories the model should
    read and apply on the NEXT call — e.g. "you sent identical prompts
    to all children, which is duplicate work." Included in the payload
    rather than swallowed so the model sees it in the same turn the
    result arrives.
    """
    if aggregate == "all":
        ok = True
    else:
        ok = bool(results and results[0].get("ok"))
    payload: dict[str, Any] = {
        "ok": ok,
        "aggregate": aggregate,
        "parent_call_id": parent_call_id,
        "results": results,
    }
    if cancelled_siblings:
        payload["cancelled_siblings"] = cancelled_siblings
    if warnings:
        payload["warnings"] = warnings
    return json.dumps(payload, indent=2)


# Default per-read cap for context_read in chars. Overridable via
# SALIENT_CONTEXT_READ_CAP=<int>. The cap stops a single context_read
# from saturating the model's context window when an agent loads a
# large findings/summary blob. When triggered, the agent gets a
# pointer to the slicing tools (context_grep / context_section / …).
_DEFAULT_CONTEXT_READ_CAP = 50_000


def _context_read_cap() -> int:
    """Resolved per-read cap (chars). Env override; falls back to default."""
    import os

    try:
        v = int(os.environ.get("SALIENT_CONTEXT_READ_CAP", "") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else _DEFAULT_CONTEXT_READ_CAP


# Regex used by _extract_targets_from_text to find IP addresses + hostnames.
# Permissive — used for "does this dispatch touch a target the operator is
# being asked about?" coordination checks, not for scope enforcement.
_IPV4_RE = _re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
_HOSTNAME_RE = _re.compile(
    r"(?<![\w.])([a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?)+)(?![\w.])",
    _re.IGNORECASE,
)


def _extract_targets_from_text(text: str) -> set[str]:
    """Extract IP addresses + hostnames from free text. Used by the
    pending-operator-question conflict check in ask_agent to detect
    when a proposed dispatch would touch a target the operator is
    being asked about. Permissive on purpose — false positives just
    mean the conflict check fires conservatively; false negatives
    mean it misses real conflicts.

    Returns a set of lower-cased tokens. IPs require all octets
    0-255; hostnames require ≥2 dot-separated labels with at least
    one alphabetic char somewhere (so version strings like '1.2.3'
    don't match, but file extensions like 'config.yaml' might —
    that's OK, they're unlikely to also appear in an operator
    question)."""
    if not text:
        return set()
    targets: set[str] = set()
    for m in _IPV4_RE.finditer(text):
        ip = m.group(1)
        try:
            if all(0 <= int(p) <= 255 for p in ip.split(".")):
                targets.add(ip)
        except ValueError:
            continue
    for m in _HOSTNAME_RE.finditer(text):
        host = m.group(1).lower()
        # Reject pure-numeric strings (already caught as IPs above
        # for valid dotted-quad; this guards against partial digits
        # like "1.2" / "100.99").
        if host.replace(".", "").replace("-", "").isdigit():
            continue
        # Require at least one alphabetic char.
        if any(c.isalpha() for c in host):
            targets.add(host)
    return targets


def _conflicting_pending_question(
    daemon: DaemonServices, prompt_targets: set[str]
) -> Question | None:
    """Return the first unresolved operator-question whose target set
    overlaps with `prompt_targets`, or None if no conflict. Used to
    refuse ask_agent dispatches that would undermine an in-flight
    operator decision about the same target."""
    if not prompt_targets:
        return None
    inbox = getattr(daemon, "inbox", None)
    questions: list[Question] = inbox.questions if inbox else []
    for q in questions:
        if q.answered or q.kind != "operator":
            continue
        q_targets = _extract_targets_from_text(q.text)
        if q_targets & prompt_targets:
            return q
    return None


__all__ = [
    # bus_tool machinery
    "bus_tool",
    "BusTool",
    "RoutedBusTool",
    "_clean_tool_schema",
    "_format_bus_validation_error",
    # Skin-module registry
    "set_bus_skin_modules",
    "_skin_module",
    # Helpers
    "_text",
    "_unwrap",
    "_delegation_gated",
    "_trust_covers",
    "_compute_ask_agent_timeout",
    "_parse_delegation_answer",
    "_parse_yes_n",
    "_redact_operator_infra",
    "_redact_secret_fields",
    "register_secret_fields",
    "register_cred_tool_markers",
    "_render_delegation_envelope",
    "_format_swarm_payload",
    "_context_read_cap",
    "_extract_targets_from_text",
    "_conflicting_pending_question",
    # Constants
    "_PER_TURN_SECS",
    "_TIMEOUT_HARD_CAP_SECS",
    "_APPROVE_WORDS",
    "_DENY_WORDS",
    "_DEFAULT_CONTEXT_READ_CAP",
    "_IPV4_RE",
    "_HOSTNAME_RE",
]
