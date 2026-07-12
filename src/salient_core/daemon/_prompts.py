"""Prompt assembly + local-model fallback helpers.

Pure module-level functions; no Daemon/AgentRunner deps. Splits into:
  • Env-var expansion for agents.yaml `${VAR}` placeholders.
  • Local-model text-shaped tool-call extraction (Hermes/Qwen/Llama).
  • Llama-3 output noise stripping.
  • Lazy + cached loaders for the three system-prompt addenda
    (agent_protocol, recipe_discipline, shadow_discipline).
  • System-prompt section formatters (`_format_tools_block`,
    `_format_approval_block`) and the context-cap heuristic.
  • DB-peek helpers used during startup before ContextStore is up.
"""

import json
import os
import re as _re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from ._helpers import normalize_swarms

# ── Thinking-tier provider seam ──────────────────────────────────────
# Some providers couple model ⇄ thinking-mode in a way the generic
# `endpoint.thinking` schema can't express (e.g. a model whose thinking
# block must be derived from the effective model+effort at dispatch, not
# read statically from config). A downstream skin registers a provider via
# `set_thinking_provider(...)`; `resolve_endpoint_thinking` consults it at
# CALL time (not import time), so registration order relative to this
# module's import is irrelevant. Default no-op claims nothing, so the
# generic static-config path always runs — the kernel has no built-in
# provider and is usable standalone. Same injection shape as
# `alias.set_active` / `set_tool_builder` / `set_prompts_root`.


def _noop_thinking_is_match(model: str | None) -> bool:
    return False


def _noop_thinking_resolve(model: str | None, effort: str | None) -> dict[str, Any]:
    return {}


_thinking_is_match: Callable[[str | None], bool] = _noop_thinking_is_match
_thinking_resolve: Callable[[str | None, str | None], dict[str, Any]] = _noop_thinking_resolve


def set_thinking_provider(
    is_match: Callable[[str | None], bool],
    resolve: Callable[[str | None, str | None], dict[str, Any]],
) -> None:
    """Register a skin's thinking-tier override provider.

    ``is_match(model)`` claims a model; when it returns True,
    ``resolve(model, effort)`` supplies the ``thinking`` block for that
    endpoint-routed agent instead of its static ``endpoint.thinking``
    config. The default no-op claims nothing. Read at call time, so a skin
    can register at import (its shim) rather than only at daemon boot."""
    global _thinking_is_match, _thinking_resolve
    _thinking_is_match = is_match
    _thinking_resolve = resolve


# Pattern for ${VAR} env-var references in agents.yaml `endpoint:` values.
# Lets the committed file carry placeholders (api_key: ${DEEPSEEK_API_KEY})
# while the actual secret lives in the operator's environment.
_ENVVAR_PATTERN = _re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_envvars(value: Any) -> Any:
    """Resolve ${VAR} references in a string against os.environ.
    Non-strings pass through unchanged. Unresolved vars stay as-is — the
    subsequent env assignment will fail loudly, which is the right
    failure mode for a missing secret."""
    if not isinstance(value, str) or "$" not in value:
        return value
    return _ENVVAR_PATTERN.sub(
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )


def endpoint_secret_gaps(
    cfgs: list[dict[str, Any]],
    env: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    """Map each UNSET ``${VAR}`` referenced by an agent's endpoint
    ``api_key`` to the sorted agent names that reference it.

    An endpoint-routed agent carries ``api_key: ${SOME_VAR}``. If
    SOME_VAR isn't in the environment, ``_expand_envvars`` leaves the
    literal ``${SOME_VAR}`` in place, the runner ships *that* as the auth
    header (x-api-key / bearer), and the agent 401s on its first model
    call with no operator-visible cause — the exact silent failure that
    stranded the MiniMax tier when MINIMAX_API_KEY was in the wrong file.
    Surfacing the gap at boot turns that silence into a loud warning.

    Empty-string values count as unset (a blank key 401s just the same).
    """
    if env is None:
        env = os.environ
    gaps: dict[str, list[str]] = {}
    for cfg in cfgs:
        endpoint = cfg.get("endpoint") or {}
        api_key = endpoint.get("api_key")
        if not isinstance(api_key, str):
            continue
        for m in _ENVVAR_PATTERN.finditer(api_key):
            var = m.group(1)
            if not env.get(var):  # missing OR empty
                gaps.setdefault(var, []).append(cfg.get("name", "<unnamed>"))
    return {var: sorted(set(names)) for var, names in gaps.items()}


def resolve_endpoint_thinking(
    endpoint_cfg: Mapping[str, Any],
    effort: str | None,
    model: str | None = None,
) -> dict[str, Any]:
    """Pick the ``thinking`` config to send for an endpoint-routed agent.

    Default is ``{type: disabled}`` — a third-party proxy often can't
    stream thinking blocks (the CLI then aborts with "Content block is
    not a thinking block"). An agent opts in via ``endpoint.thinking``.

    When a registered thinking provider (see ``set_thinking_provider``)
    claims the model, model and thinking-type are coupled in a way the
    static schema can't express, so the block is derived from the
    *effective* ``model`` + ``effort`` at dispatch rather than read from
    ``endpoint.thinking``. That keeps a runtime model swap correct without
    re-editing agents.yaml. No provider is registered by default.

    Otherwise (DeepSeek, Ollama/LiteLLM, …) the static config wins: for
    the legacy ``adaptive`` mode map ``effort: low`` → off (``disabled``),
    anything else → ``adaptive``; ``enabled`` + budget and explicit
    ``disabled`` pass through unchanged at any effort.
    """
    if _thinking_is_match(model):
        return _thinking_resolve(model, effort)
    thinking = dict(endpoint_cfg.get("thinking") or {"type": "disabled"})
    if thinking.get("type") == "adaptive" and effort == "low":
        return {"type": "disabled"}
    return thinking


# ── Local-model text-shaped tool calls ───────────────────────────────
#
# Regex tuple for JSON-as-text fallback (see AgentRunner._dispatch_text_function_calls).
# Each pattern targets a wrapper shape some local models emit when they decide to
# "call" a function but can't produce native Anthropic tool_use blocks. The bare
# JSON object pattern is intentionally last — it's the loosest and would otherwise
# eat content meant for tagged shapes.

# Llama 3.x sometimes emits a `<|python_tag|>` marker before what it thinks
# is a tool invocation. It's pure output-format noise — the operator never
# wants to see it. Stripped per-line for both inline and prefix cases.
_LLAMA_NOISE_TOKENS = (
    "<|python_tag|>",
    "<|eot_id|>",
    "<|start_header_id|>assistant<|end_header_id|>",
)


def _strip_llama_output_noise(text: str) -> str:
    """Remove llama-3 model-format tokens that leak into text replies when
    a local model emits them through LiteLLM (e.g. when its tool-call
    attempt got coerced back into the message stream)."""
    if not text:
        return text
    out = text
    for tok in _LLAMA_NOISE_TOKENS:
        out = out.replace(tok, "")
    return out


_FN_CALL_PATTERNS = (
    # Hermes / Qwen style: <tool_call>{"name":"...","arguments":{...}}</tool_call>
    _re.compile(
        r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
        _re.DOTALL,
    ),
    # Llama 3.x function tag:  <function=NAME>{"...": ...}</function>
    _re.compile(
        r"<function=([a-zA-Z_][\w.:-]*)>\s*(\{.*?\})\s*</function>",
        _re.DOTALL,
    ),
    # Bare JSON object with a "name" key and either "parameters" or "arguments".
    # Allow nested braces in the args via a non-greedy match anchored by the
    # closing brace at the end. Stays single-line-friendly via DOTALL.
    _re.compile(
        r"\{\s*\"name\"\s*:\s*\"([a-zA-Z_][\w.:-]*)\"\s*,\s*"
        r"\"(?:parameters|arguments|input)\"\s*:\s*(\{.*?\})\s*\}",
        _re.DOTALL,
    ),
)


def _extract_function_calls_from_text(
    text: str,
) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    """Find OpenAI-style function-call JSON embedded in a text reply.

    Returns ([(name, args), ...], stripped_text). `stripped_text` is the
    input with every matched block removed so the visible reply (after
    dispatch) doesn't expose raw JSON to the operator.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    remaining = text
    for pat in _FN_CALL_PATTERNS:
        # iterate spans so we can splice them out as we go
        new_parts: list[str] = []
        cursor = 0
        for m in pat.finditer(remaining):
            new_parts.append(remaining[cursor : m.start()])
            cursor = m.end()
            try:
                if len(m.groups()) == 1:
                    # Wrapped JSON object — name is inside it
                    obj = json.loads(m.group(1))
                    name = obj.get("name")
                    args = obj.get("parameters") or obj.get("arguments") or obj.get("input") or {}
                else:
                    # <function=NAME>{...}</function>: name is in group 1,
                    # raw args object in group 2.
                    name = m.group(1)
                    args = json.loads(m.group(2))
            except (json.JSONDecodeError, ValueError, AttributeError):
                # Malformed JSON — leave the original text in place so the
                # operator can see what the model tried to emit.
                new_parts.append(remaining[m.start() : m.end()])
                continue
            if isinstance(name, str) and isinstance(args, dict):
                out.append((name, args))
        new_parts.append(remaining[cursor:])
        remaining = "".join(new_parts)
    return out, remaining


# ── Prompt addendum loaders ──────────────────────────────────────────
#
# The three addenda ship inside the package (`salient_core/prompts/`,
# declared in package-data) so they resolve identically from a source
# checkout and an installed wheel.

# The three universal addenda ship inside the package by default. A
# downstream that vendors its own (domain-framed) addenda — or whose daemon
# runs from an installed wheel where the package dir isn't its repo — calls
# ``set_prompts_root(path)`` at startup to point the loader elsewhere. Same
# idiom as ``alias.set_active`` / ``daemon.set_tool_builder``. Paths resolve
# against ``_prompts_root`` at LOAD time (not import), so the setter takes
# effect as long as it runs before the first load.
_DEFAULT_PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"
_prompts_root = _DEFAULT_PROMPTS_ROOT

_AGENT_PROTOCOL_CACHE: str | None = None
_RECIPE_DISCIPLINE_CACHE: str | None = None
_SHADOW_DISCIPLINE_CACHE: str | None = None


def set_prompts_root(path: Path | str) -> None:
    """Point the universal-addendum loader at a different directory and clear
    the caches. Called once at startup by a downstream skin; must run before
    the first prompt load."""
    global _prompts_root, _AGENT_PROTOCOL_CACHE, _RECIPE_DISCIPLINE_CACHE
    global _SHADOW_DISCIPLINE_CACHE
    _prompts_root = Path(path)
    _AGENT_PROTOCOL_CACHE = None
    _RECIPE_DISCIPLINE_CACHE = None
    _SHADOW_DISCIPLINE_CACHE = None


def _read_addendum(name: str) -> str:
    path = _prompts_root / name
    if not path.exists():
        raise FileNotFoundError(
            f"prompt addendum {name!r} not found under {_prompts_root} — set "
            f"the prompts root via salient_core.daemon.set_prompts_root(path)."
        )
    return path.read_text()


def _load_agent_protocol() -> str:
    """Load the universal task-protocol addendum (cached)."""
    global _AGENT_PROTOCOL_CACHE
    if _AGENT_PROTOCOL_CACHE is None:
        _AGENT_PROTOCOL_CACHE = _read_addendum("agent_protocol.md")
    return _AGENT_PROTOCOL_CACHE


def _load_recipe_discipline() -> str:
    """Load the recipe/writeup discipline addendum (cached). Injected into
    every agent's system prompt so context_write / evidence-file / chat
    writeups stay factual and procedural — plain descriptions of what was
    done and observed, free of dramatized framing."""
    global _RECIPE_DISCIPLINE_CACHE
    if _RECIPE_DISCIPLINE_CACHE is None:
        _RECIPE_DISCIPLINE_CACHE = _read_addendum("recipe_discipline.md")
    return _RECIPE_DISCIPLINE_CACHE


def _load_shadow_discipline() -> str:
    """Load the shadow-agent discipline block (cached). Injected into the
    system prompt of any agent with a `substitute_for:` field — keeps
    shadow agents from over-probing on methodology questions and from
    fumbling tool schemas, both of which inflate cost and latency without
    improving the answer."""
    global _SHADOW_DISCIPLINE_CACHE
    if _SHADOW_DISCIPLINE_CACHE is None:
        _SHADOW_DISCIPLINE_CACHE = _read_addendum("shadow_discipline.md")
    return _SHADOW_DISCIPLINE_CACHE


def resolve_per_agent_prompts(
    cfgs: list[dict[str, Any]] | dict[str, dict[str, Any]],
    config_dir: Path,
) -> None:
    """For each agent cfg with a `system_prompt_path` field, read the
    file (relative to config_dir) and write its contents into
    `cfg['system_prompt']`.

    Mutates in place. Uncached so `reset <agent>` after a .md edit
    picks up the change on the next call (lessons follow the same
    pattern). Fail-loud when the path is set but the file is missing
    — silent fallthrough has historically been an operator-debug
    nightmare. Cfgs without `system_prompt_path` are left alone; the
    sim harness and unit tests build programmatic cfgs with an inline
    `system_prompt` field, and that path stays valid.
    """
    iterable = cfgs.values() if isinstance(cfgs, dict) else cfgs
    for cfg in iterable:
        path_field = cfg.get("system_prompt_path")
        if not path_field:
            continue
        prompt_file = (config_dir / path_field).resolve()
        try:
            cfg["system_prompt"] = prompt_file.read_text()
        except FileNotFoundError as e:
            raise ValueError(
                f"agent {cfg.get('name')!r} declares "
                f"`system_prompt_path: {path_field!r}` but the file does "
                f"not exist at {prompt_file}"
            ) from e


# ── System-prompt section formatters + context cap ───────────────────


def _format_tools_block(cfg: dict[str, Any]) -> str:
    """Tell the agent which tools are open — primary MCP, enabled built-ins,
    and the common built-ins it should NOT try (so it doesn't reach for
    Edit/Write/etc. from training)."""
    from ._tool_registry import get_tool_wire_names

    TOOL_WIRE_NAMES = get_tool_wire_names()
    tool_cfg = cfg.get("tool") or {}
    builtins = list(cfg.get("builtin_tools") or [])
    lines: list[str] = []
    if tool_cfg.get("type"):
        tt = tool_cfg["type"]
        wire = TOOL_WIRE_NAMES.get(tt, "?")
        lines.append(
            f"- PRIMARY action tool: MCP tool `{cfg['name']}.{wire}` "
            f"(type={tt}). Prefer this for task work that needs system "
            "effects — it routes through the daemon's guards (sandbox, "
            "destructive-command refusal, persistent shell session, etc.)."
        )
    if builtins:
        lines.append(
            f"- Built-in Claude Code tools available: {', '.join(builtins)}. "
            "Use these when they're the natural choice (e.g. `Read` for "
            "files, `Grep` for searching, `Bash` for one-off shell "
            "commands that don't need session state)."
        )
    always_listed = {"Task", "Agent"}
    disabled = [
        t
        for t in ("Bash", "Read", "Grep", "Write", "Edit", "WebFetch", "WebSearch")
        if t not in builtins and t not in always_listed
    ]
    if disabled:
        lines.append(
            f"- NOT available (do not attempt): {', '.join(disabled)}. Calls to these will fail."
        )
    if not lines:
        return ""
    return "Your available tools:\n" + "\n".join(lines)


_SWARM_BOOTSTRAP_ADDENDA: list[str] = []


def register_swarm_bootstrap_addendum(text: str) -> None:
    """Append domain guidance to the SWARM orchestrator prompt body. The kernel
    ships generic swarm guidance; a downstream skin registers extra bootstrap
    advice specific to its domain at startup. Call-time registration, read when
    the prompt is built."""
    _SWARM_BOOTSTRAP_ADDENDA.append(text)


def build_swarm_orchestrator_prompt(
    source: str,
    members: list[str],
    decomp_snippet: str | None = None,
    *,
    ephemeral: bool = False,
    orchestrator_name: str | None = None,
    member_max_turns_floor: int | None = None,
    composition: list[dict[str, Any]] | None = None,
) -> str:
    """Generated system_prompt for a SWARM orchestrator agent.

    The orchestrator is a pure router: given a task, decompose it into
    disjoint slices and dispatch one slice per member via a single
    ``ask_agents`` call. Members are forks of ``source`` and carry that
    agent's full tool surface — the orchestrator itself owns no primary
    tool.

    The ``decomp_snippet`` is an optional per-source hint pulled from
    that agent's ``swarm_decomp:`` field (e.g. an indexer → "key-range
    partitions"; bash → "file-list shards"). When absent the generic
    decomposition guidance below is the only steering.

    Mixed swarms: pass ``composition`` as
    ``[{"source": str, "members": [str, ...], "decomp": str | None,
       "floor": int | None}, ...]`` to describe a heterogeneous roster.
    The intro block names every sub-roster, every source's
    ``swarm_decomp`` hint is stitched in, and ``source`` itself is
    treated as a display sentinel ("mixed"). Single-source swarms leave
    ``composition`` as ``None`` and the prompt reads exactly as before.

    When ``ephemeral`` is True, the prompt adds a lifecycle block telling
    the orchestrator to save raw findings + the synthesis via
    context_write under stable keys BEFORE replying, and that the swarm
    will auto-tear-down once the caller-facing reply lands. The daemon
    persists the same artifacts redundantly on disk + context — the
    prompt encourages structured saves so the synthesis is well-shaped
    for downstream re-use."""
    N = len(members)
    roster = ", ".join(f"`{m}`" for m in members)
    orch_label = orchestrator_name or "<you>"
    is_mixed = composition is not None and len(composition) > 1
    if is_mixed:
        assert composition is not None  # narrowed by is_mixed; for the type-checker
        # Mixed swarm intro: list each sub-roster + the source's
        # discipline so the orchestrator knows it's coordinating
        # specialists, not clones. Routes work to whichever group has
        # the right tools rather than treating all members alike.
        sub_rosters = "\n".join(
            f"  - `{g['source']}` × {len(g.get('members') or [])} → "
            + ", ".join(f"`{m}`" for m in (g.get("members") or []))
            for g in composition
        )
        blocks: list[str] = [
            f"You are a MIXED SWARM orchestrator coordinating {N} workers "
            f"across {len(composition)} distinct sources. Your only job is "
            f"to take an incoming task, split it into disjoint slices that "
            f"play to each worker's discipline, and dispatch them IN "
            f"PARALLEL via a single `mcp__bus__{orch_label}__ask_agents` "
            f"call. When all replies are back, synthesize them into one "
            f"answer to the caller.",
            f"Roster (group by source — each source has its own tool "
            f"surface and discipline):\n{sub_rosters}\n"
            f"Address members by these exact names — the source agents "
            f"themselves are NOT in this swarm.",
        ]
    else:
        blocks = [
            f"You are a SWARM orchestrator for {N} forked workers of "
            f"`{source}`. Your only job is to take an incoming task, split "
            f"it into {N} disjoint slices, and dispatch one slice to each "
            f"worker IN PARALLEL via a single `mcp__bus__{orch_label}__ask_agents` "
            f"call. When all replies are back, synthesize them into one "
            f"answer to the caller.",
            f"Workers: {roster}. Each is an independent fork of `{source}` "
            f"and has the SAME tool surface and discipline as the source. "
            f"Address them by these exact names — they will not respond to "
            f"`{source}` itself.",
        ]
    blocks.extend(
        [
            "Decomposition rules:\n"
            f"- Each child MUST receive a DIFFERENT prompt. Identical prompts "
            f"waste parallelism (use one `ask_agent` instead).\n"
            f"- Slices must be DISJOINT — no overlapping work between "
            f"siblings. Overlap is the failure mode that erases the speedup.\n"
            f"- Size slices so the longest one is roughly the shortest one. "
            f"Skewed slices wedge the whole swarm behind one straggler.\n"
            f"- If the task can't decompose into {N} pieces, decompose into "
            f"fewer and skip the rest — don't fabricate filler work.\n"
            f"- Never include yourself in the children list. Never call a "
            f"non-worker agent from this swarm — route those via the caller.",
            # Slot-assignment / divergence rule. Pinned in tests because LLM
            # workers sharing the same base prompt converge on the same
            # tools / words / approaches by default (shared training
            # distribution). Pre-assignment is the cheapest mitigation:
            # the orchestrator names a distinct primary tool per slot
            # BEFORE dispatching, so workers don't independently
            # rediscover the same starting move.
            "Slot-assignment divergence — REQUIRED:\n"
            f"BEFORE you fan out, pre-assign a DISTINCT primary tool / "
            f"approach / angle to each of the {N} member slots. State the "
            f"assignment explicitly in each child's prompt (e.g. \"your "
            f'primary tool is X; siblings are using Y, Z, …"). Convergence '
            f"— ≥3 members using the same primary tool or producing the "
            f"same first move — is a failure of decomposition, NOT a "
            f"property of the task. Workers running the same base model "
            f"share a training distribution; without explicit slot "
            f"assignment they will independently land on the same\n"
            f"obvious choice. For tool-bearing sources, enumerate the "
            f"source's tool surface and bind one tool per slot. For "
            f"reasoning sources, bind one ANGLE per slot (different "
            f"hypothesis class, different lens, different framing). The "
            f"per-source decomposition hint below — when present — lists "
            f"a tool/angle pool you can draw from.",
        ]
    )
    # Mixed-roster slot-assignment addendum. Single-source swarms can
    # rely on the source's swarm_decomp pool; mixed swarms benefit from
    # an explicit reminder that the source-boundary is the first slot
    # axis — bind work to whichever group has the right tools, then
    # diversify within the group.
    if is_mixed:
        blocks.append(
            "Mixed-roster slot-assignment — REQUIRED:\n"
            "Your slot axes are nested: first pick which source-group is "
            "best suited to each slice (don't ask a narrow `search` fork to "
            "reason about multi-step synthesis), THEN apply distinct-primary-tool "
            "divergence WITHIN each group. A mixed swarm wastes its main "
            "advantage when every group does the same kind of work — let "
            "specialists specialize."
        )
    # Per-child turn budget — wire-level signal from the source agent's
    # cfg (`swarm_member_max_turns`). When the source declares a floor,
    # the orchestrator MUST pass at least this in `ask_agents` so deep-
    # workflow tasks (deep analysis, multi-stage tooling, iterative probing)
    # don't hit the runner's hard turn cap mid-run. Past failure
    # observed 2026-05-19: a deep-workflow swarm given 6-10 per child by
    # default produced PARTIAL findings on every member (cap fired at turn
    # 18-22, mid-analysis).
    if member_max_turns_floor and member_max_turns_floor > 0:
        # For mixed swarms the floor is the MAX across all sources so
        # the slowest discipline still gets its budget — phrase it as
        # "the deepest workflow" rather than naming one source.
        workflow_label = "deepest member workflow" if is_mixed else f"`{source}` workflow"
        blocks.append(
            f"Per-child turn budget — REQUIRED FLOOR:\n"
            f"When you fan out via `ask_agents`, pass `max_turns: "
            f"{member_max_turns_floor}` (or higher — never lower) for "
            f"each child. The {workflow_label} needs this much "
            f"budget to complete (multi-step tool chains: enumerate → "
            f"analyze → report). Below the floor, members hit the "
            f"runner's hard turn cap mid-work and return PARTIAL "
            f"findings — a wasted dispatch. Bump higher when the task "
            f"is unusually deep (multi-binary analysis, large target "
            f"surface); never go below."
        )
    # Shared bootstrap context — saves every member from re-discovering
    # the same setup facts. The orchestrator writes one context key
    # BEFORE fanning out; members read it first.
    orch_key = orchestrator_name or "<orchestrator>"
    blocks.append(
        "Shared bootstrap (recommended for non-trivial tasks):\n"
        f"BEFORE you fan out, write a one-shot `context_write` to key "
        f"`swarm:{orch_key}/bootstrap` with the facts every member "
        f"would otherwise discover independently — target name(s), "
        f"file paths, ports, scope notes, any pre-computed common "
        f"context. In each child's "
        f"prompt, include the line "
        f"\"first call context_read('{orch_key}', 'swarm:{orch_key}/"
        f"bootstrap') to get the shared starting context\". This "
        f"converts N independent discovery passes into one "
        f"shared one, freeing each member's turn budget for the "
        f"slice you actually want them to do."
    )
    if is_mixed:
        # One per-source guidance block per group that supplied a
        # swarm_decomp snippet. The orchestrator gets a labeled pool
        # per discipline so slot-assignment can draw from each.
        for g in composition or []:
            snippet = g.get("decomp")
            if not isinstance(snippet, str) or not snippet.strip():
                continue
            blocks.append(
                f"Source-specific decomposition guidance for "
                f"`{g.get('source')}`:\n{snippet.strip()}"
            )
    elif decomp_snippet:
        blocks.append(
            f"Source-specific decomposition guidance for `{source}`:\n{decomp_snippet.strip()}"
        )
    blocks.append(
        "Synthesis: when collecting replies, merge them into one coherent "
        "answer. Call out which slice contributed each finding so the "
        "caller can attribute and re-dispatch if any slice failed. If a "
        "slice errored, surface that in the synthesis — don't silently "
        "drop it."
    )
    # Ephemeral-mode lifecycle block. Pinned by tests/test_swarm.py.
    if ephemeral:
        orch_key = orchestrator_name or "<orchestrator>"
        blocks.append(
            "EPHEMERAL SWARM — single-task lifecycle:\n"
            "This swarm tears itself down when your job completes. "
            "Before you reply to the caller, persist findings + "
            "synthesis so the work survives teardown:\n"
            f"  1. For each child reply, write the raw finding to "
            f"`context_write` under key "
            f"`swarm:{orch_key}/findings/<member-name>` — one entry "
            f"per worker. Keep the body verbatim (don't paraphrase); "
            f"the synthesis is a separate artifact.\n"
            f"  2. Write your synthesis to `context_write` under key "
            f"`swarm:{orch_key}/synthesis`. This is the artifact other "
            f"agents (and future you) will read via context_read.\n"
            f"  3. Then reply to the caller with the synthesis text. "
            f"Once your reply lands, the daemon writes synthesis.md + "
            f"findings/<member>.md to the engagement's swarms/{orch_key}/ "
            f"directory and stops the orchestrator + every member.\n"
            f"  4. If you need to end the swarm BEFORE replying to the "
            f"caller (task aborted, synthesis impossible), call the "
            f"`swarm_finish` bus tool with a `reason` and end your turn."
        )
    else:
        # Non-ephemeral: still teach saving + swarm_finish, but as
        # operator-elective rather than auto-fired. Persistent swarms
        # often want findings preserved across multiple dispatches.
        orch_key = orchestrator_name or "<orchestrator>"
        blocks.append(
            "Persistent SWARM — multi-task lifecycle:\n"
            "This swarm stays alive across multiple dispatches. After "
            "each synthesis, persist the artifacts so context_read "
            "callers and future-you can re-find them:\n"
            f"  - Each child finding → `context_write` "
            f"`swarm:{orch_key}/findings/<member-name>` (overwrite per "
            f"dispatch; use task-tagged keys if you want history).\n"
            f"  - Each synthesis → `context_write` "
            f"`swarm:{orch_key}/synthesis`.\n"
            f"  - To explicitly end the swarm + cascade-prune the "
            f"members, call the `swarm_finish` bus tool with a "
            f"`reason`. The daemon writes synthesis.md + findings/* "
            f"to the engagement directory on teardown."
        )
    blocks.extend(_SWARM_BOOTSTRAP_ADDENDA)
    return "\n\n".join(blocks)


def _peek_swarms(db_path: Path | None) -> dict[str, dict[str, Any]]:
    """Read the persisted swarm registry from the DB. Returns
    ``{orchestrator_name: {"source": str, "members": [...],
    "composition": [{"source": str, "members": [...]} ...]?}}``.
    Empty dict when DB unset, key missing, or value malformed. Used at
    daemon startup so killing/restoring a swarm survives a bounce.

    The ``composition`` field is optional — entries persisted before
    mixed-swarm support landed only carry ``source`` + ``members`` and
    still restore. Mixed swarms set ``source`` to ``"mixed"`` and rely
    on ``composition`` to recover each member's actual origin."""
    if db_path is None or not db_path.exists():
        return {}
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'swarms'").fetchone()
        if not row or not row[0]:
            return {}
        # Shape validation lives in the shared `normalize_swarms`
        # contract (salient/daemon/_helpers.py) — the Daemon boot-
        # hydration path uses the same function, so the two readers
        # can't drift. It also unwraps the versioned envelope.
        return normalize_swarms(json.loads(row[0]))
    except (sqlite3.Error, json.JSONDecodeError):
        return {}


# The window an agent inherits when it runs on the Claude Code / SDK
# default model (i.e. `effective_model` returned None — no per-agent or
# per-engagement override). The current default is the 1M-context opus
# (`claude-opus-4-8[1m]`), so a default-model agent has a 1M window — NOT
# the legacy 200k. Operators can pin any value per-agent via `context_cap`.
_DEFAULT_MODEL_CONTEXT_CAP = 1_000_000


def _context_cap_for(model: str | None, cfg: dict[str, Any]) -> int:
    """Estimate the context-window cap (tokens) for a model. Used to render
    a "context %" metric — the operator's signal that an agent's accumulated
    conversation is approaching its window and should be reset/compacted.

    Per-agent override via `context_cap` in the agent config beats the
    heuristic. An agent with no explicit model runs on the Claude Code
    default (currently the 1M-context opus), so None → 1M. Explicit ids:
    1M for any opus-4-7/4-8 id, the bare `opus` alias (floats to latest
    opus), or any id carrying the `1m` window marker; 200k otherwise."""
    explicit = cfg.get("context_cap")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    if not model:
        return _DEFAULT_MODEL_CONTEXT_CAP
    m = model.lower()
    if "opus-4-7" in m or "opus-4-8" in m or "1m" in m or m in ("opus", "opus-4.7", "opus-4.8"):
        return 1_000_000
    return 200_000


def _peek_active_engagement(db_path: Path | None) -> str | None:
    """Read the active engagement run_id from the DB without instantiating
    a full ContextStore. Returns None when the DB is unset, missing, or
    doesn't yet have the `meta` table (older DB created before this layer)."""
    if db_path is None or not db_path.exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'active_engagement_run_id'"
            ).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def _peek_running_agents(db_path: Path | None) -> list[str]:
    """Read the previously-running agent name list from the DB. Used at
    startup when resuming an engagement so the same set comes back up
    without an interactive picker. Returns [] when the DB is unset, the
    key is missing, or the value is malformed."""
    if db_path is None or not db_path.exists():
        return []
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'running_agents'").fetchone()
        if not row or not row[0]:
            return []
        names = json.loads(row[0])
        return [n for n in names if isinstance(n, str)]
    except (sqlite3.Error, json.JSONDecodeError):
        return []


def _peek_spawned_cfgs(db_path: Path | None) -> dict[str, dict]:
    """Read the previously-spawned agent cfgs from the DB.

    Spawned agents (planner, reviewer, anything from
    ``salientctl spawn``) live in ``self.runners`` but NOT in
    ``self.all_cfgs`` — their configs come from template YAML at spawn
    time, not from agents.yaml. Without this lookup, daemon restart
    loses the cfg and the lead can't come back up; pre-fix the entry
    code reported them as 'skipped (not in agents.yaml)' and the
    operator had to manually re-spawn after every daemon bounce.

    Returns ``{name: cfg_dict}``; ``{}`` when DB unset, key missing,
    or value malformed."""
    if db_path is None or not db_path.exists():
        return {}
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'spawned_agent_cfgs'").fetchone()
        if not row or not row[0]:
            return {}
        data = json.loads(row[0])
        if not isinstance(data, dict):
            return {}
        return {
            n: c
            for n, c in data.items()
            if isinstance(n, str) and isinstance(c, dict) and c.get("name")
        }
    except (sqlite3.Error, json.JSONDecodeError):
        return {}


def _format_approval_block(policy: dict[str, Any]) -> str:
    approve_before = policy.get("approve_before") or []
    delegate_gate = policy.get("approve_before_delegate")
    if not approve_before and not delegate_gate:
        return ""
    parts: list[str] = []
    if approve_before:
        actions = ", ".join(approve_before)
        parts.append(
            "Operator approval gate:\n"
            f"These actions are operator-gated by the daemon: {actions}. A "
            "matching tool call will BLOCK at the wire until the operator "
            "approves — you don't need a separate <ask_operator>; just make the "
            "call normally and the daemon surfaces it (tool + command/args) for "
            "approval. The operator may reply 'yes' (proceed), 'no [reason]' "
            "(the call is refused and returns an error — pivot, don't retry "
            "blindly), or 'edit: <new command>' (proceed with the edited "
            "command). State your intent clearly in the call so the operator "
            "can decide without re-asking."
        )
    if delegate_gate:
        if delegate_gate is True or delegate_gate in ("*", ["*"]):
            scope = "every agent you might delegate to"
        elif isinstance(delegate_gate, (list, tuple, set)):
            scope = "these target agents: " + ", ".join(sorted(map(str, delegate_gate)))
        else:
            scope = f"target agent {delegate_gate!r}"
        parts.append(
            "Delegation approval gate:\n"
            f"`ask_agent` calls to {scope} are operator-gated by the bus — "
            "the tool will block until the operator answers. You don't need "
            "to send a separate <ask_operator>; just call ask_agent normally "
            "and the bus will surface the proposal (target + prompt) for "
            "approval. The operator may reply 'yes' (forward as-is), "
            "'no [reason]' (denied — the tool returns an error), or "
            "'edit: <new prompt>' (forward the edited prompt). State the "
            "purpose of the delegation clearly in the prompt so the operator "
            "can decide without re-asking."
        )
    block = "\n\n".join(parts)
    if "sudo" in approve_before:
        block += (
            "\n\nSudo specifically:\n"
            "- Issue the EXACT sudo command (binary + flags + target) — the "
            "daemon blocks it for approval and, once approved, runs it with "
            "confirm_destructive set for you. No separate ask, no retry.\n"
            "- The daemon does not have a TTY for password entry. The "
            "operator must have a primed sudo timestamp cache. If a sudo "
            "call fails with 'a password is required' / 'a terminal is "
            "required' / 'no askpass program', the cache is empty: stop, "
            "do NOT retry, and surface this to the operator with a clear "
            "instruction to run `salientctl elevate` from their own "
            "terminal. Resume only after they confirm the cache is "
            "primed."
        )
    return block
