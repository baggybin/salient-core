"""Bus context_* tools — read/write/search across agents' shared
key-value namespace.

Extracted from salient/bus.py during the package split. Each @tool
closure here was previously nested inside `make_bus()` in the
monolith; the per-group factory pattern (mirroring salient/tools/)
keeps the closure shape so daemon/owner capture is identical.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import AfterValidator, BaseModel, Field, field_validator

from ._common import *  # noqa: F401,F403
from ._common import bus_tool

if TYPE_CHECKING:
    from ..protocols import DaemonServices


# Shared OPTIONAL `key` for the read-side context tools (read + the slicers):
# `key` has a documentable default ("latest" = the agent's most recent value),
# so per the de-require litmus it is OPTIONAL on the wire — leaving it required
# reintroduces the "key is a required property" small-model refusal bug (a
# smaller model drops the whole tool call rather than supply a property it
# can't reason about). context_write keeps `key` REQUIRED (a write with no key
# is meaningless). The AfterValidator maps a blank value to "latest" as the
# single source of truth (the handler needs no `or "latest"` coalesce); a
# skipped default isn't validated, so the omitted case is supplied by default=.
_OptionalKey = Annotated[
    str,
    Field(
        default="latest",
        description="Context key; defaults to 'latest' (the agent's most recent "
        "value) when omitted or empty.",
    ),
    AfterValidator(lambda v: v or "latest"),
]


# Wire schema for context_read (bus reconciliation step B). Reference exemplar
# for the migration's default-handling pattern:
#   * The explanation is a COMMENT, not a docstring — Pydantic promotes a model
#     docstring into the schema's root `description`, drifting the golden.
#   * `key` defaults to "latest" so it is OPTIONAL on the wire — the SDK
#     shorthand wrongly marked it required (the "key is a required property"
#     small-model bug).
#   * bus_tool STRIPS the `default` keyword from the wire schema, so the
#     model-visible default and the golden's change-tripwire live in the field
#     DESCRIPTION instead ("defaults to 'latest'"): a future default change must
#     edit the description, which the golden captures. Semantic defaults get a
#     description; neutral defaults (0/""/False) don't need one.
#   * The falsy→canonical mapping is a `field_validator` — the SINGLE source of
#     truth — so the handler no longer needs its own `or "latest"` coalesce.
#     Defaults skip validation, so the default supplies the omitted case and the
#     validator only normalizes a PROVIDED empty string.
class _ContextReadArgs(BaseModel):
    agent: str
    key: str = Field(
        default="latest",
        description="Context key to read; defaults to 'latest' (the agent's most "
        "recent reply) when omitted or empty.",
    )

    @field_validator("key")
    @classmethod
    def _blank_key_is_latest(cls, v: str) -> str:
        return v or "latest"


# Wire schemas for the context_* tools. Each numeric optional's default doc now
# lives in its field `description` (relocated from the tool prose, so it sits by
# the `default=` and the golden captures it), and its floor is a `ge=` constraint
# (surfacing as `minimum` in the wire schema) that REPLACES the handlers' old
# `max(N, ...)` clamps + `int(... or N)` coalesces — one source of truth, and an
# explicit out-of-range/0 value is now honored/corrected rather than silently
# overridden.
class _ContextWriteArgs(BaseModel):
    key: str
    value: str


class _ContextListArgs(BaseModel):
    # De-required (mirrors list_agents.filter): the description documents empty
    # ⇒ all keys, a value worth defaulting to, so {} lists everything.
    filter: str = Field("", description="empty / '*' ⇒ all keys; an agent name narrows.")


class _ContextGrepArgs(BaseModel):
    agent: str
    key: _OptionalKey
    pattern: str
    before: int = Field(2, ge=0, description="Lines BEFORE each match; defaults to 2.")
    after: int = Field(2, ge=0, description="Lines AFTER each match; defaults to 2.")
    max_matches: int = Field(20, ge=1, description="Cap on match count; defaults to 20.")


class _ContextSectionArgs(BaseModel):
    agent: str
    key: _OptionalKey
    around: str
    before: int = Field(10, ge=0, description="Lines before each hit; defaults to 10.")
    after: int = Field(10, ge=0, description="Lines after each hit; defaults to 10.")
    max_matches: int = Field(1, ge=1, description="Cap on occurrences; defaults to 1.")


class _ContextHeadArgs(BaseModel):
    agent: str
    key: _OptionalKey
    n: int = Field(20, ge=1, description="Line count; defaults to 20.")


class _ContextTailArgs(BaseModel):
    agent: str
    key: _OptionalKey
    n: int = Field(20, ge=1, description="Line count; defaults to 20.")


class _ContextLinesArgs(BaseModel):
    agent: str
    key: _OptionalKey
    # Optional (defaults to 1 = the first line) — a documentable default, so it
    # stays off the required list (and out of the small-model "required property"
    # refusal trap). Matches the handler's historical `int(args.get("start") or 1)`.
    start: int = Field(1, ge=1, description="First line, 1-indexed; defaults to 1.")
    # Dynamic default: end defaults to start (a cross-field default). The sentinel
    # is None — NOT falsy-0 — so an explicit end is honored; the handler resolves
    # None→start and clamps end>=start (a cross-field rule ge can't express).
    end: int | None = Field(None, ge=1, description="Last line, inclusive; defaults to start.")


class _ContextCountArgs(BaseModel):
    agent: str
    key: _OptionalKey
    pattern: str


class _ContextSummaryArgs(BaseModel):
    agent: str
    key: _OptionalKey


def make_context_tools(daemon: DaemonServices, owner: str) -> list:
    """Returns the 10 context_* @tool-decorated closures for this owner."""

    @bus_tool(
        "context_write",
        "Write a value to YOUR OWN context namespace so other agents can read it. "
        "Use short keys like 'findings' or 'summary'. Values must be strings.",
        _ContextWriteArgs,
    )
    async def context_write(args: dict[str, Any]) -> dict[str, Any]:
        from ..alias import rewrite_inbound as _alias_inbound

        key = (args.get("key") or "").strip()
        value = args.get("value")
        if not key:
            return _text("error: key is required", error=True)
        if not isinstance(value, str):
            return _text("error: value must be a string", error=True)
        # Stored value comes from Claude (containing aliases). Reverse
        # aliases → real names before persisting so operator-facing
        # reads see consistent data. Round-trips correctly: on next
        # read, rewrite_outbound puts aliases back in for Claude.
        value = _alias_inbound(value)
        daemon.context.write(owner, key, value)
        return _text(f"wrote {owner}/{key} ({len(value)} chars)")

    @bus_tool(
        "context_read",
        "Read a value from another agent's context namespace. "
        "Common keys: 'latest' (their most recent reply), 'findings', 'summary'. "
        "Use the context_list tool to discover what's available. "
        "The returned value is wrapped in a <context_value agent=... key=...> "
        "tag so you can identify what it is and where it came from. "
        "Large values are truncated; when you need a specific slice, prefer "
        "the dedicated slicers — context_grep / context_section / "
        "context_head / context_tail / context_lines / context_summary — "
        "which return only the lines you ask for instead of the whole blob.",
        _ContextReadArgs,
    )
    async def context_read(args: dict[str, Any]) -> dict[str, Any]:
        from ..alias import rewrite_outbound as _alias_outbound
        from ..alias import to_real as _alias_to_real
        from ..daemon import _wrap_context_value  # avoid circular at import

        agent = (args.get("agent") or "").strip()
        agent = _alias_to_real(agent)  # alias → real for lookup
        # `key` is model-normalized: the field default covers an omitted key and
        # the field_validator maps an explicit "" — both to "latest". So the
        # handler trusts the validated value; no coalesce here (single source).
        key = args["key"]
        v = daemon.context.read(agent, key) or ""
        cap = _context_read_cap()
        full_len = len(v)
        if full_len > cap:
            # Don't inline ANY of the content when over cap — the agent
            # has dedicated slicers (grep/section/head/tail/lines/count/
            # summary) that can pull exactly what's needed. Returning a
            # prefix would just burn tokens. Hand back metadata + a
            # pointer to the slicers and let the agent pick its query.
            lines = v.splitlines()
            first = lines[0] if lines else ""
            last = lines[-1] if lines else ""
            v = (
                f"[value too large to inline ({full_len} chars, "
                f"{len(lines)} lines, cap {cap}).\n"
                f"  first line: {first!r}\n"
                f"  last line:  {last!r}\n"
                f"Use one of these on ({agent!r}, {key!r}) to extract a slice:\n"
                f"  context_summary  — char/line counts + first/last line\n"
                f"  context_head     — first N lines\n"
                f"  context_tail     — last N lines\n"
                f"  context_grep     — regex/substring + surrounding context\n"
                f"  context_section  — anchor text + surrounding lines\n"
                f"  context_lines    — explicit line range\n"
                f"  context_count    — match count only (cheap probe)\n"
                f"]"
            )
        return _text(_alias_outbound(_wrap_context_value(agent, key, v)))

    @bus_tool(
        "context_list",
        "List stored context keys. Pass an agent name to filter, or '' / '*' for all.",
        _ContextListArgs,
    )
    async def context_list(args: dict[str, Any]) -> dict[str, Any]:
        from ..alias import rewrite_outbound as _alias_outbound
        from ..alias import to_real as _alias_to_real

        flt = args["filter"].strip()  # default "" ⇒ all keys
        agent = None if flt in ("", "*") else _alias_to_real(flt)
        rows = daemon.context.list_entries(agent)
        if not rows:
            return _text("(no entries)")
        lines = [f"{r['agent']}/{r['key']}  ({r['size']} chars)  {r['preview']!r}" for r in rows]
        return _text(_alias_outbound("\n".join(lines)))

    # ── context slicers ────────────────────────────────────────────────────
    # Stateless slice operations over an already-stored context value.
    # Use these instead of context_read when you only need part of a large
    # blob — keeps the prompt small and the per-turn cost low.

    def _ctx_get(agent: str, key: str) -> tuple[str, str, str]:
        """Resolve (real_agent, key, value). Translates alias → real for
        the agent param so the new slicers work with aliased names too."""
        from ..alias import to_real as _alias_to_real

        a = _alias_to_real((agent or "").strip())
        k = (key or "latest").strip()
        v = daemon.context.read(a, k) or ""
        return a, k, v

    @bus_tool(
        "context_grep",
        "Search a stored context value for a pattern; return matching "
        "lines with optional surrounding context. `pattern` is a Python "
        "regex (use \\b for word boundaries; escape special chars if you "
        "want literal).\n"
        "  agent       — REQUIRED. Source agent.\n"
        "  key         — REQUIRED. Key under the source agent.\n"
        "  pattern     — REQUIRED. Regex.\n"
        "  before      — Optional. Lines BEFORE each match.\n"
        "  after       — Optional. Lines AFTER each match.\n"
        "  max_matches — Optional. Cap on match count.\n"
        "Output is formatted like grep -n with line numbers so you can "
        "follow up with context_lines.",
        _ContextGrepArgs,
    )
    async def context_grep(args: dict[str, Any]) -> dict[str, Any]:
        import re as _re

        from ..alias import rewrite_outbound as _alias_outbound

        pattern = args["pattern"]
        if not pattern:
            return _text("error: pattern is required", error=True)
        try:
            rx = _re.compile(pattern)
        except _re.error as e:
            return _text(f"error: invalid regex: {e}", error=True)
        # before/after/max_matches are model-validated ints with ge= floors —
        # trust them directly (no clamp/coalesce).
        before = args["before"]
        after = args["after"]
        max_matches = args["max_matches"]

        agent, key, value = _ctx_get(args["agent"], args["key"])
        if not value:
            return _text(f"(no value at {agent}/{key})")
        lines = value.splitlines()
        emitted: set[int] = set()
        out_blocks: list[str] = []
        n_matches = 0
        for i, ln in enumerate(lines):
            if rx.search(ln):
                n_matches += 1
                if n_matches > max_matches:
                    break
                lo = max(0, i - before)
                hi = min(len(lines), i + after + 1)
                block_lines = []
                for j in range(lo, hi):
                    if j in emitted:
                        continue
                    sep = ":" if j == i else "-"
                    block_lines.append(f"{j + 1:>5}{sep}{lines[j]}")
                    emitted.add(j)
                if block_lines:
                    out_blocks.append("\n".join(block_lines))
        if not out_blocks:
            return _text(f"(no matches for {pattern!r} in {agent}/{key})")
        header = (
            f"# context_grep {agent}/{key}  pattern={pattern!r}  "
            f"matches={n_matches}{'+ (capped)' if n_matches > max_matches else ''}"
        )
        return _text(_alias_outbound(header + "\n" + "\n--\n".join(out_blocks)))

    @bus_tool(
        "context_section",
        "Find an anchor substring in a stored context value and return "
        "the surrounding lines. Useful when you know a target IP / "
        "hostname / identifier and want the few lines around the first (or "
        "first N) occurrences.\n"
        "  agent       — REQUIRED. Source agent.\n"
        "  key         — REQUIRED. Key under the source agent.\n"
        "  around      — REQUIRED. Anchor substring to find.\n"
        "  before      — Optional. Lines before each hit.\n"
        "  after       — Optional. Lines after each hit.\n"
        "  max_matches — Optional. Cap on occurrences.",
        _ContextSectionArgs,
    )
    async def context_section(args: dict[str, Any]) -> dict[str, Any]:
        from ..alias import rewrite_outbound as _alias_outbound

        anchor = args["around"]
        if not anchor:
            return _text("error: around is required", error=True)
        before = args["before"]
        after = args["after"]
        max_matches = args["max_matches"]

        agent, key, value = _ctx_get(args["agent"], args["key"])
        if not value:
            return _text(f"(no value at {agent}/{key})")
        lines = value.splitlines()
        emitted: set[int] = set()
        out_blocks: list[str] = []
        n_matches = 0
        for i, ln in enumerate(lines):
            if anchor in ln:
                n_matches += 1
                if n_matches > max_matches:
                    break
                lo = max(0, i - before)
                hi = min(len(lines), i + after + 1)
                block_lines = []
                for j in range(lo, hi):
                    if j in emitted:
                        continue
                    sep = ":" if j == i else "-"
                    block_lines.append(f"{j + 1:>5}{sep}{lines[j]}")
                    emitted.add(j)
                if block_lines:
                    out_blocks.append("\n".join(block_lines))
        if not out_blocks:
            return _text(f"(no occurrences of {anchor!r} in {agent}/{key})")
        header = f"# context_section {agent}/{key}  around={anchor!r}  matches={n_matches}"
        return _text(_alias_outbound(header + "\n" + "\n--\n".join(out_blocks)))

    @bus_tool(
        "context_head",
        "Return the first N lines of a stored context value. Cheap way "
        "to peek at structure (headers, format) without loading the "
        "whole thing.\n"
        "  agent — REQUIRED. Source agent.\n"
        "  key   — REQUIRED. Key under the source agent.\n"
        "  n     — Optional. Line count.",
        _ContextHeadArgs,
    )
    async def context_head(args: dict[str, Any]) -> dict[str, Any]:
        from ..alias import rewrite_outbound as _alias_outbound

        n = args["n"]  # model-validated int, ge=1
        agent, key, value = _ctx_get(args["agent"], args["key"])
        if not value:
            return _text(f"(no value at {agent}/{key})")
        lines = value.splitlines()
        slice_ = lines[:n]
        body = "\n".join(f"{i + 1:>5}:{ln}" for i, ln in enumerate(slice_))
        header = f"# context_head {agent}/{key}  showing {len(slice_)}/{len(lines)} lines"
        return _text(_alias_outbound(header + "\n" + body))

    @bus_tool(
        "context_tail",
        "Return the last N lines of a stored context value. Useful for "
        "findings appended over time — see the latest entries without "
        "loading the rest.\n"
        "  agent — REQUIRED. Source agent.\n"
        "  key   — REQUIRED. Key under the source agent.\n"
        "  n     — Optional. Line count.",
        _ContextTailArgs,
    )
    async def context_tail(args: dict[str, Any]) -> dict[str, Any]:
        from ..alias import rewrite_outbound as _alias_outbound

        n = args["n"]  # model-validated int, ge=1
        agent, key, value = _ctx_get(args["agent"], args["key"])
        if not value:
            return _text(f"(no value at {agent}/{key})")
        lines = value.splitlines()
        start = max(0, len(lines) - n)
        slice_ = lines[start:]
        body = "\n".join(f"{start + i + 1:>5}:{ln}" for i, ln in enumerate(slice_))
        header = (
            f"# context_tail {agent}/{key}  showing {len(slice_)}/{len(lines)} "
            f"lines (from line {start + 1})"
        )
        return _text(_alias_outbound(header + "\n" + body))

    @bus_tool(
        "context_lines",
        "Return an explicit line range (1-indexed, inclusive) of a stored "
        "context value. Use this after context_grep tells you a match is "
        "at line 142 and you want to see the broader block around it.\n"
        "  agent — REQUIRED. Source agent.\n"
        "  key   — REQUIRED. Key under the source agent.\n"
        "  start — REQUIRED. First line (1-indexed).\n"
        "  end   — Optional. Last line, inclusive.",
        _ContextLinesArgs,
    )
    async def context_lines(args: dict[str, Any]) -> dict[str, Any]:
        from ..alias import rewrite_outbound as _alias_outbound

        start = args["start"]  # model-validated int, ge=1
        # Dynamic default resolved by None-sentinel (never truthiness), then the
        # cross-field clamp end>=start (a rule ge= can't express on one field).
        end = args["end"] if args["end"] is not None else start
        end = max(start, end)
        agent, key, value = _ctx_get(args["agent"], args["key"])
        if not value:
            return _text(f"(no value at {agent}/{key})")
        lines = value.splitlines()
        lo = start - 1
        hi = min(len(lines), end)
        if lo >= len(lines):
            return _text(f"(start={start} past end of {agent}/{key} ({len(lines)} lines))")
        slice_ = lines[lo:hi]
        body = "\n".join(f"{lo + i + 1:>5}:{ln}" for i, ln in enumerate(slice_))
        header = f"# context_lines {agent}/{key}  lines {start}-{lo + len(slice_)} of {len(lines)}"
        return _text(_alias_outbound(header + "\n" + body))

    @bus_tool(
        "context_count",
        "Count matches of a regex pattern in a stored context value. "
        "Returns the number only — no content. Use this as a cheap probe "
        "before deciding whether to run context_grep.",
        _ContextCountArgs,
    )
    async def context_count(args: dict[str, Any]) -> dict[str, Any]:
        import re as _re

        pattern = args.get("pattern") or ""
        if not pattern:
            return _text("error: pattern is required", error=True)
        try:
            rx = _re.compile(pattern)
        except _re.error as e:
            return _text(f"error: invalid regex: {e}", error=True)
        agent, key, value = _ctx_get(args.get("agent") or "", args.get("key") or "")
        if not value:
            return _text(f"(no value at {agent}/{key})")
        n_lines_matched = sum(1 for ln in value.splitlines() if rx.search(ln))
        n_total_matches = len(rx.findall(value))
        return _text(
            f"# context_count {agent}/{key}  pattern={pattern!r}\n"
            f"lines_with_match={n_lines_matched}\n"
            f"total_match_occurrences={n_total_matches}"
        )

    @bus_tool(
        "context_summary",
        "Return metadata about a stored context value without its body: "
        "char count, line count, first line, last line. Use this to "
        "triage entries before loading them.",
        _ContextSummaryArgs,
    )
    async def context_summary(args: dict[str, Any]) -> dict[str, Any]:
        from ..alias import rewrite_outbound as _alias_outbound

        agent, key, value = _ctx_get(args.get("agent") or "", args.get("key") or "")
        if not value:
            return _text(f"(no value at {agent}/{key})")
        lines = value.splitlines()
        first = lines[0] if lines else ""
        last = lines[-1] if lines else ""
        return _text(
            _alias_outbound(
                f"# context_summary {agent}/{key}\n"
                f"chars={len(value)}\n"
                f"lines={len(lines)}\n"
                f"first_line={first!r}\n"
                f"last_line={last!r}"
            )
        )

    return [
        context_write,
        context_read,
        context_list,
        context_grep,
        context_section,
        context_head,
        context_tail,
        context_lines,
        context_count,
        context_summary,
    ]
