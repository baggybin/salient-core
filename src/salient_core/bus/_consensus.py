"""Bus consensus tool — ask_consensus (Experts Mix).

`ask_consensus(name, prompt)` asks the SAME prompt to an agent and its shadow
(the primary↔shadow pair, or an explicit N-agent panel), then returns a
structured comparison: an agreement score, the corroborated atoms (≥2 agents
agree), and the divergent ones (single-source — flag for follow-up).

It is the multi-leg cousin of `ask_partner`: ADVISORY (a second opinion / cross-
check, not new task work), so legs dispatch with `_skip_redispatch_gate` and the
tool does NOT trip the swarm fan-out operator-approval gate. Each leg still runs
through `ask_agent`, so scope / cross-team / engagement-disabled / cycle checks
all compose. Synthesis is deterministic (entity overlap + token similarity); the
`counsel` agent (Mode B) is an OPT-IN LLM judge. The tool is pure — it returns
the comparison and never writes the KG; the caller decides any `kg_assert`.
"""

from __future__ import annotations

import asyncio
import difflib
import itertools
import json
import re
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..memory.embeddings import cosine, get_embedder
from ..policy import safeguards
from ._common import *  # noqa: F401,F403  (brings in _text, _extract_targets_from_text)
from ._common import bus_tool
from ._flags import BusFlags

if TYPE_CHECKING:
    from ..protocols import DaemonServices

DEFAULT_JUDGE = "counsel"


class _AskConsensusArgs(BaseModel):
    name: str
    prompt: str
    # agents: falsy ([]) ⇒ resolve_panel takes the default primary+shadow pair
    # (it branches on `if explicit:`), so an empty-list default is behavior-
    # identical to the old None. Neutral default → no field description.
    agents: list[str] = Field(default_factory=list)
    # judge/judge_agent carry SEMANTIC defaults (docs in their descriptions).
    # judge is the wire enum; Literal reproduces AND enforces it (the handler's
    # old `.lower()` leniency is gone — a non-enum value is a clean error).
    judge: Literal["auto", "on", "off"] = Field(
        "auto",
        description="'on'=always reconcile; 'auto'=only when agreement is low; "
        "'off'=never. Defaults to 'auto'.",
    )
    judge_agent: str = Field(
        DEFAULT_JUDGE,
        description="Which running agent reconciles the panel; defaults to 'counsel'.",
    )
    # neutral: 0 / "" mean "no hint" / "no constraint".
    max_turns: int = 0
    deliverable: str = ""

    @field_validator("judge_agent")
    @classmethod
    def _blank_judge_agent_is_default(cls, v: str) -> str:
        # Single source of truth for the strip + fallback the handler used to do.
        return v.strip() or DEFAULT_JUDGE


# ─── deterministic atom extraction + agreement scoring (pure) ────────────────

_PORT_RE = re.compile(r"\b(\d{1,5})/(?:tcp|udp)\b", re.IGNORECASE)
_PORT_WORD_RE = re.compile(r"\bports?\s+(\d{1,5})\b", re.IGNORECASE)
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)
_HASH_RE = re.compile(r"\b[0-9a-f]{32,64}\b", re.IGNORECASE)
_ATOM_KINDS = ("ip", "host", "port", "cve", "hash")


def _is_ipv4(token: str) -> bool:
    parts = token.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _atoms(text: str) -> dict[str, set[str]]:
    """Extract comparable atoms from one agent reply, bucketed by kind.
    Reuses `_extract_targets_from_text` (IPs + hostnames) and adds ports,
    CVE ids, and hex hashes. Precise over greedy — a bare number isn't a
    port unless tagged `N/tcp` or `port N`."""
    text = text or ""
    targets = _extract_targets_from_text(text)
    ips = {t for t in targets if _is_ipv4(t)}
    return {
        "ip": ips,
        "host": targets - ips,
        "port": (
            {m.group(1) for m in _PORT_RE.finditer(text)}
            | {m.group(1) for m in _PORT_WORD_RE.finditer(text)}
        ),
        "cve": {m.group(0).upper() for m in _CVE_RE.finditer(text)},
        "hash": {m.group(0).lower() for m in _HASH_RE.finditer(text)},
    }


def _agreement(
    texts: dict[str, str],
) -> tuple[float, dict[str, list[str]], dict[str, dict[str, str]]]:
    """Score agreement across ≥2 agent replies.

    Returns (score, corroborated, divergent):
      - corroborated[kind] = atoms present in ≥2 legs.
      - divergent[kind]    = {atom: the_one_leg_that_said_it}.
      - score = |corroborated atoms| / |all distinct atoms|. When NO atoms
        were extracted (prose-only answers), falls back to the average
        pairwise difflib ratio so the score isn't trivially 0.
    """
    names = list(texts)
    if len(names) < 2:
        return 0.0, {}, {}
    per = {n: _atoms(texts[n]) for n in names}
    corroborated: dict[str, list[str]] = {}
    divergent: dict[str, dict[str, str]] = {}
    all_atoms: set[tuple[str, str]] = set()
    corr_atoms: set[tuple[str, str]] = set()
    for kind in _ATOM_KINDS:
        owners: dict[str, list[str]] = {}
        for n in names:
            for a in per[n][kind]:
                owners.setdefault(a, []).append(n)
        corr = sorted(a for a, who in owners.items() if len(who) >= 2)
        div = sorted(a for a, who in owners.items() if len(who) == 1)
        if corr:
            corroborated[kind] = corr
        if div:
            divergent[kind] = {a: owners[a][0] for a in div}
        for a, who in owners.items():
            all_atoms.add((kind, a))
            if len(who) >= 2:
                corr_atoms.add((kind, a))
    if all_atoms:
        score = len(corr_atoms) / len(all_atoms)
    else:
        ratios = [
            difflib.SequenceMatcher(None, texts[a], texts[b]).ratio()
            for a, b in itertools.combinations(names, 2)
        ]
        score = sum(ratios) / len(ratios) if ratios else 0.0
    return score, corroborated, divergent


# ─── general semantic agreement (embedding-based, domain-agnostic) ───────────


async def semantic_agreement(texts: dict[str, str], embedder: Any) -> float | None:
    """Average pairwise cosine similarity across agent answers — a domain-general
    convergence score in ``[0,1]``.

    Complements the deterministic atom score: `_agreement` only sees security
    atoms (IPs/ports/CVEs) and collapses prose to a raw string ratio, so two
    answers that say the same thing in different words score near 0. This embeds
    each answer and measures meaning, not surface tokens. Zero-norm vectors
    (e.g. a punctuation-only answer that embeds to all zeros) are excluded
    rather than averaged in as cosine 0, so one garbage leg can't silently drag
    the convergence score down. Returns ``None`` when embeddings are unavailable
    or fewer than two answers embed to a usable vector (the caller then falls
    back to the deterministic score) — it never raises.
    """
    named = [n for n in texts if (texts.get(n) or "").strip()]
    if embedder is None or len(named) < 2:
        return None
    try:
        vecs = await embedder.embed([texts[n] for n in named])
    except Exception:  # noqa: BLE001 — embeddings must never break a consensus run
        return None
    if not vecs or len(vecs) != len(named):
        return None
    kept = [v for v in vecs if v and any(x != 0.0 for x in v)]
    if len(kept) < 2:
        return None
    sims = [cosine(kept[i], kept[j]) for i, j in itertools.combinations(range(len(kept)), 2)]
    if not sims:
        return None
    avg = sum(sims) / len(sims)
    return max(0.0, min(1.0, avg))  # cosine can dip negative; read it as a fraction


# ─── per-leg reasoning trace (for the judge + the return payload) ────────────

# Event kinds the runner records that reveal an agent's reasoning trail; the
# prompt echo (`user_message`) and peer chatter are dropped so the trace is the
# agent's own work.
_TRACE_KINDS = ("thinking", "tool_call", "tool_result", "text", "reply")


def _snippet(content: Any, cap: int = 240) -> str:
    if isinstance(content, dict):
        for key in ("text", "summary", "result", "args", "_raw"):
            v = content.get(key)
            if isinstance(v, str) and v.strip():
                content = v
                break
        else:
            content = json.dumps(content, default=str)
    text = str(content).strip().replace("\n", " ")
    return text[:cap] + ("…" if len(text) > cap else "")


def _leg_trace(
    daemon: Any,
    agent: str,
    since_ts: float,
    limit: int = 40,
    job_id: int | None = None,
) -> list[dict[str, Any]]:
    """Compact reasoning trace for one panel leg: the thinking/tool/text events
    that agent recorded after `since_ts`. When ``job_id`` is known it scopes the
    query to that dispatch, so concurrent unrelated jobs on the same agent can't
    contaminate the trace. Returns ``[]`` when no event store is wired (e.g. a
    minimal daemon in tests) or the query fails — never raises."""
    ctx = getattr(daemon, "context", None)
    if ctx is None or not hasattr(ctx, "query_events"):
        return []
    try:
        events = ctx.query_events(agent=agent, since_ts=since_ts, job_id=job_id, limit=limit)
    except Exception:  # noqa: BLE001 — trace capture is best-effort telemetry
        return []
    steps: list[dict[str, Any]] = []
    for e in events:
        if e.get("kind") not in _TRACE_KINDS:
            continue
        steps.append(
            {
                "kind": e.get("kind"),
                "tool": e.get("tool"),
                "ts": e.get("ts"),
                "text": _snippet(e.get("content")),
            }
        )
    return steps


def _trace_digest(trace: list[dict[str, Any]]) -> str:
    """One-line-per-step rendering of a leg trace, for the judge prompt."""
    lines = []
    for s in trace:
        tag = s["kind"] + (f"[{s['tool']}]" if s.get("tool") else "")
        lines.append(f"    · {tag}: {s['text']}")
    return "\n".join(lines)


# ─── reply unwrap ────────────────────────────────────────────────────────────
# `_unwrap` is single-sourced in _common (shared with ask_agents._run_child) and
# arrives via the `from ._common import *` above. Used at the two panel call sites
# below.


_JUDGE_PROMPT = (
    "CONSENSUS JUDGE (Mode B). {n} agents were asked the SAME task; each answer "
    "is shown with the reasoning trace that produced it. Reconcile them into ONE "
    "answer: state what they AGREE on (high confidence), flag where they DIVERGE "
    "and which is more credible + why (use the traces — an answer backed by "
    "checked work beats an asserted one), and call out anything assumed but not "
    "evidenced. Be compact.\n\n"
    "ORIGINAL TASK:\n{task}\n\n{bodies}"
)


# ─── panel resolution (shared by the bus tool + the operator command) ────────


def canonical_primary(daemon: DaemonServices, real_name: str) -> str:
    """Map a name to its canonical primary (a shadow → the agent it substitutes
    for; a primary → itself)."""
    cfg = daemon.all_cfgs.get(real_name) or {}
    return cfg.get("substitute_for") or real_name


def running_shadow_of(daemon: DaemonServices, primary: str) -> str | None:
    """The first RUNNING shadow declared `substitute_for: <primary>`, or None."""
    all_cfgs: dict[str, Any] = daemon.all_cfgs or {}
    for sname, scfg in all_cfgs.items():
        if (scfg or {}).get("substitute_for") == primary:
            r = daemon.runners.get(sname)
            if r is not None and r.status != "stopped":
                return sname
    return None


def resolve_panel(
    daemon: DaemonServices,
    name: str,
    explicit: Any = None,
    exclude: str | None = None,
) -> tuple[list[str], str | None]:
    """Resolve the consensus panel to a list of distinct, LIVE agent names.

    With `explicit` (a list) → that N-way panel (aliases resolved). Otherwise
    the canonical primary of `name` + its one running shadow. `exclude` drops
    the caller (the bus tool excludes `owner`; the operator command excludes
    nothing). Returns `(resolved, error)` — `error` is set only on a malformed
    `explicit` arg; an under-2 panel is left for the caller to message.
    """
    from ..alias import to_real as _alias_to_real

    if explicit:
        if not isinstance(explicit, list):
            return [], "'agents' must be a list of agent names"
        panel = [_alias_to_real(str(a).strip()) for a in explicit if str(a).strip()]
    else:
        primary = canonical_primary(daemon, _alias_to_real(name))
        shadow = running_shadow_of(daemon, primary)
        panel = [primary] + ([shadow] if shadow else [])

    resolved: list[str] = []
    seen: set[str] = set()
    for a in panel:
        if a == exclude or a in seen:
            continue
        seen.add(a)
        if a not in daemon.all_cfgs:
            continue
        r = daemon.runners.get(a)
        if r is None or r.status == "stopped":
            continue
        resolved.append(a)
    return resolved, None


def make_consensus_tools(daemon: DaemonServices, owner: str, ask_agent: Any) -> list:
    """Returns [ask_consensus]. `ask_agent` is the delegation tool built by
    make_delegation_tools — consensus dispatches each leg through it so all
    per-call safeguards (scope, cross-team, cycle, engagement-disabled) compose.
    """

    async def _dispatch_one(
        agent_name: str, prompt: str, max_turns: Any, deliverable: str
    ) -> dict[str, Any]:
        # `job_capture` is an in-process write-back sink on the typed flags
        # channel: ask_agent writes the child Job id into it so the trace query
        # below can be scoped to THIS dispatch. It rides on BusFlags (not args)
        # so the write-back aliases this dict — model_dump on the args model
        # would sever it. Legs go through .trusted (routed), never the wire.
        cap: dict[str, Any] = {}
        child: dict[str, Any] = {
            "name": agent_name,
            "prompt": prompt,
            # prefer_primary so a named primary leg isn't re-routed to its own
            # shadow (we dispatch the shadow separately).
            "prefer_primary": True,
        }
        if max_turns:
            child["max_turns"] = max_turns
        if deliverable:
            child["deliverable"] = deliverable
        try:
            # Advisory second-opinion — exempt from the redispatch governor.
            reply = await ask_agent.trusted(
                child, flags=BusFlags(skip_redispatch_gate=True, job_capture=cap)
            )
        except Exception as exc:  # noqa: BLE001 — one leg's fault ≠ whole call
            return {"name": agent_name, "ok": False, "error": str(exc), "job_id": cap.get("job_id")}
        ok, text = _unwrap(reply)
        return {
            "name": agent_name,
            "ok": ok,
            ("result" if ok else "error"): text,
            "job_id": cap.get("job_id"),
        }

    async def _run_judge(
        task: str, ok_results: list[dict[str, Any]], judge_agent: str
    ) -> tuple[str | None, str]:
        """Best-effort reconciliation by `judge_agent` (default `counsel`). Only
        runs when the judge is ALREADY running (so we never trip its agent-start
        operator gate), the judge isn't the caller, and the judge isn't itself on
        the panel. Each leg's reasoning trace is included so the judge weighs
        checked work over bare assertion.

        Returns ``(judge_text, skip_reason)`` — ``judge_text`` is None whenever
        the judge didn't produce a verdict, and ``skip_reason`` says exactly why
        (so a forced ``judge="on"`` gets an accurate warning, not a generic
        "unavailable")."""
        panel_names = {r["name"] for r in ok_results}
        if owner == judge_agent:
            return None, f"judge {judge_agent!r} is the caller — excluded"
        if judge_agent in panel_names:
            return None, f"judge {judge_agent!r} is on the panel — excluded to stay impartial"
        r = daemon.runners.get(judge_agent)
        if r is None or r.status == "stopped":
            return None, f"judge {judge_agent!r} is not running"
        parts = []
        for res in ok_results:
            block = f"=== {res['name']} ===\n{res.get('result', '')}"
            digest = _trace_digest(res.get("trace") or [])
            if digest:
                block += f"\n  reasoning trace:\n{digest}"
            parts.append(block)
        body = _JUDGE_PROMPT.format(n=len(ok_results), task=task, bodies="\n\n".join(parts))
        try:
            reply = await ask_agent.trusted(
                {
                    "name": judge_agent,
                    "prompt": body,
                    # The judge was validated by literal name above — don't let
                    # ask_agent substitute-route it to a shadow model.
                    "prefer_primary": True,
                },
                flags=BusFlags(skip_redispatch_gate=True),
            )
        except Exception:  # noqa: BLE001 — judge is best-effort
            return None, f"judge {judge_agent!r} errored"
        ok, text = _unwrap(reply)
        if ok and text.strip():
            return text.strip(), ""
        return None, f"judge {judge_agent!r} returned no usable reply"

    @bus_tool(
        "ask_consensus",
        "Ask the SAME prompt to an agent AND its shadow (or an explicit panel) "
        "and get back a structured comparison — agreement score, corroborated "
        "findings (≥2 agents agree), and divergences (single-source, flag for "
        "follow-up). Use it for a high-stakes finding you want a second model to "
        "corroborate before you act on it. Advisory: no operator approval, no "
        "redispatch counting; each agent still runs through the normal "
        "scope/cross-team/cycle checks.\n"
        "\n"
        "  name        — REQUIRED. The agent (or its role) to seek consensus on; "
        "its running shadow becomes the second opinion.\n"
        "  prompt      — REQUIRED. The task — sent verbatim to every panel member.\n"
        "  agents      — Optional. Explicit list of agent names for an N-way "
        "expert mix; overrides the default primary+shadow pair.\n"
        "  judge       — Optional. 'on' = always reconcile; "
        "'auto' = only when agreement is low; 'off' = never.\n"
        "  judge_agent — Optional. Which running agent "
        "reconciles the panel — point it at a different model to judge.\n"
        "  max_turns   — Optional. Per-leg soft turn hint (same as ask_agent).\n"
        "  deliverable — Optional. One-line acceptance criterion for each leg.\n"
        "\n"
        "Returns JSON: {ok, panel, agreement_score, semantic_score, corroborated, "
        "divergent, per_agent:[{name, ok, result|error, trace}], judge}. Needs ≥2 "
        "distinct live agents (the caller is always excluded); errors otherwise.",
        _AskConsensusArgs,
    )
    async def ask_consensus(args: dict[str, Any]) -> dict[str, Any]:
        name = (args.get("name") or "").strip()
        prompt = args.get("prompt") or ""
        if not name or not prompt:
            return _text("error: name and prompt are required", error=True)
        judge_agent = args["judge_agent"]  # model-normalized (strip + fallback)

        # restrict_swarm_tools agents (local-LLM test surfaces) can't fan out.
        caller_cfg = daemon.all_cfgs.get(owner) or {}
        if caller_cfg.get("restrict_swarm_tools"):
            return _text(
                f"error: ask_consensus refused — caller {owner!r} has "
                f"`restrict_swarm_tools: true`. Use ask_agent for a single "
                f"dispatch instead.",
                error=True,
            )

        judge_mode = args["judge"]  # model-validated enum: auto | on | off
        max_turns = args["max_turns"]  # 0 ⇒ no hint (falsy in _dispatch_one)
        deliverable = args["deliverable"].strip()

        # ── Resolve the panel (caller excluded) ──────────────────────────────
        resolved, panel_err = resolve_panel(daemon, name, args["agents"], exclude=owner)
        if panel_err:
            return _text(f"error: {panel_err}", error=True)

        if len(resolved) < 2:
            return _text(
                f"error: ask_consensus needs ≥2 distinct live agents to compare; "
                f"resolved {resolved or '[]'} (caller {owner!r} excluded). Start "
                f"the shadow (e.g. a deepseek_* substitute) or pass agents:[...] "
                f"with ≥2 running agents.",
                error=True,
            )

        # ── Cycle check (same as ask_agents) ─────────────────────────────────
        from ..coord.delegation_graph import find_cycles_for_edges, format_cycle

        existing = list((daemon._bus_calls or {}).values())
        cycles = find_cycles_for_edges(existing, owner, resolved)
        bad = [(n, p) for n, p in cycles.items() if p is not None]
        if bad:
            return _text(
                "error: ask_consensus refused — would close a delegation cycle: "
                + "; ".join(f"{n}: {format_cycle(p)}" for n, p in bad),
                error=True,
            )

        # ── Dispatch all legs concurrently ───────────────────────────────────
        # One wall-clock mark before the fan-out bounds every leg's trace query;
        # panel members are distinct agents, and each trace is additionally
        # scoped to its own child job id (when known) so a concurrent unrelated
        # job on the same agent can't leak events into this panel's traces.
        dispatch_ts = time.time()
        per_agent = list(
            await asyncio.gather(
                *[_dispatch_one(a, prompt, max_turns, deliverable) for a in resolved]
            )
        )
        for r in per_agent:
            r["trace"] = _leg_trace(daemon, r["name"], dispatch_ts, job_id=r.pop("job_id", None))
        ok_results = [r for r in per_agent if r.get("ok")]

        warnings: list[str] = []
        if len(ok_results) < len(resolved):
            warnings.append(
                f"{len(resolved) - len(ok_results)} of {len(resolved)} agents "
                f"did not return a usable reply"
            )

        # ── Synthesis: deterministic atoms + general semantic score ──────────
        texts = {r["name"]: r.get("result", "") for r in ok_results}
        score, corroborated, divergent = _agreement(texts)
        semantic_score = await semantic_agreement(
            texts, get_embedder(getattr(daemon, "profile", None))
        )

        # ── Optional LLM judge ───────────────────────────────────────────────
        # The atom and semantic scores live on different scales (sparse overlap
        # vs embedding cosine), so each has its own threshold; either one
        # signalling divergence invokes the judge. The semantic trigger is
        # additive — it can only add judge runs on top of the atom decision,
        # never suppress one.
        judge_text: str | None = None
        if len(ok_results) >= 2:
            profile = getattr(daemon, "profile", None)
            threshold = safeguards.consensus_auto_judge_below_from_profile(profile)
            semantic_threshold = safeguards.consensus_auto_judge_below_semantic_from_profile(
                profile
            )
            diverges = score < threshold or (
                semantic_score is not None and semantic_score < semantic_threshold
            )
            want_judge = judge_mode == "on" or (judge_mode == "auto" and diverges)
            if want_judge:
                judge_text, judge_skip = await _run_judge(prompt, ok_results, judge_agent)
                if judge_text is None and judge_mode == "on":
                    warnings.append(f"judge requested but {judge_skip}")

        payload: dict[str, Any] = {
            "ok": len(ok_results) >= 2,
            "panel": resolved,
            "agreement_score": round(score, 4),
            "semantic_score": round(semantic_score, 4) if semantic_score is not None else None,
            "corroborated": corroborated,
            "divergent": divergent,
            "per_agent": per_agent,
            "judge": judge_text,
        }
        if warnings:
            payload["warnings"] = warnings
        return _text(json.dumps(payload, indent=2))

    return [ask_consensus]
