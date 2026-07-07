"""Bus audit / policy tools — rule_validate, read_evidence, prior_actions.

Pre-action policy validation + retrospective inspection of recorded
tool calls. Extracted from salient/bus.py during the package split.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ._common import *  # noqa: F401,F403
from ._common import bus_tool

if TYPE_CHECKING:
    from ..protocols import DaemonServices


# Wire schemas for the audit tools. rule_kind stays a plain str (the handler
# lowercases + membership-checks against the linter table; a Literal would drop
# that leniency and add an enum the wire schema never had). since_minutes is
# int|None because its absent value must be None ("unbounded" — 0 would mean
# "since now"). Numeric floors/ceilings are ge=/le= (model-visible `minimum`/
# `maximum`, replacing the handlers' max() clamps); semantic defaults carry a
# field description; neutral defaults (0/""/False) stay plain.
class _RuleValidateArgs(BaseModel):
    rule_kind: str
    rule_text: str


class _ReadEvidenceArgs(BaseModel):
    sha: str
    offset: int = Field(0, ge=0)
    length: int = Field(
        8192, ge=1, le=32768, description="bytes to read; defaults to 8192, max 32768 per call."
    )


class _PriorActionsArgs(BaseModel):
    target: str = ""
    tool: str = ""
    since_minutes: int | None = Field(
        None, ge=1, description="only actions newer than N minutes; unbounded when omitted."
    )
    limit: int = Field(20, ge=1, description="max rows to return; defaults to 20.")
    include_args: bool = False


# ── rule_validate linter backends ────────────────────────────────────
# Detection-rule draft linting. Each kind shells out to a local CLI that
# parses/compiles the rule and reports syntax + structural errors. The
# binary is detected at call time (shutil.which) so a missing linter
# degrades to an actionable "install X" message rather than a hard
# feature gap. Invocations were locked empirically:
#   sigma     → `sigma check` (sigma-cli): exit 0 = valid (best-practice
#               "issues" don't fail under the default --pass-on-issues);
#               exit≠0 = parse/condition error. FilenameLengthIssue is an
#               artifact of our temp filename, so it's excluded.
#   yara      → `yarac <rule> /dev/null`: compiles, discards output;
#               exit≠0 on a syntax/compile error, slow-string warnings
#               don't fail.
#   suricata  → `suricata -T -S <rule> -l <dir>`: config-test mode over
#               only this rule file. (Engine is a system package; absent
#               on many hosts → the missing-binary path.)
_RULE_LINTERS: dict[str, dict[str, str]] = {
    "sigma": {"bin": "sigma", "suffix": ".yml", "install": "pipx install sigma-cli"},
    "yara": {
        "bin": "yarac",
        "suffix": ".yar",
        "install": "install the 'yara' package (e.g. `apt install yara`)",
    },
    "suricata": {
        "bin": "suricata",
        "suffix": ".rules",
        "install": "install suricata (e.g. `apt install suricata`)",
    },
}

_LINT_TIMEOUT_S = 15.0
_LINT_OUTPUT_CAP = 4096


def _lint_argv(kind: str, rule_path: str, work_dir: str) -> list[str]:
    if kind == "sigma":
        # `filename_length` is a best-practice validator that always trips
        # on our temp filename — exclude it (real errors/condition-errors
        # still fail). `sigma list validators` for the full set.
        return ["sigma", "check", "--exclude", "filename_length", rule_path]
    if kind == "yara":
        return ["yarac", rule_path, os.devnull]
    # suricata
    return ["suricata", "-T", "-S", rule_path, "-l", work_dir]


async def _run_rule_linter(
    kind: str,
    body: str,
    timeout: float = _LINT_TIMEOUT_S,
) -> tuple[bool, str]:
    """Lint a detection-rule draft via the local CLI for ``kind``.

    Returns ``(ok, report)``. ``ok`` is False on a parse/compile error, a
    missing linter binary, or a timeout. The report is human-readable and,
    on every failure path, says what to do next — so the caller is never
    taught the 'validate → error → give up' reflex.
    """
    spec = _RULE_LINTERS[kind]
    binary = spec["bin"]
    if shutil.which(binary) is None:
        return (
            False,
            f"linter {binary!r} is not installed on the daemon host, so this "
            f"{kind} rule was NOT validated (it may still be correct). "
            f"To enable automatic linting: {spec['install']}. Meanwhile, "
            f"review the draft manually or hand it to `threathunt`.",
        )
    with tempfile.TemporaryDirectory(prefix="salient_rulelint_") as work_dir:
        rule_path = os.path.join(work_dir, f"rule{spec['suffix']}")
        with open(rule_path, "w") as fh:
            fh.write(body)
        try:
            proc = await asyncio.create_subprocess_exec(
                *_lint_argv(kind, rule_path, work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work_dir,
            )
        except FileNotFoundError:
            return (False, f"linter {binary!r} vanished between detection and exec")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            with suppress(ProcessLookupError):
                await proc.wait()
            return (False, f"{binary} timed out after {timeout:.0f}s linting the {kind} rule")
        # Scrub temp paths so the agent sees clean diagnostics.
        text = out.decode(errors="replace").strip()
        text = text.replace(rule_path, f"<{kind}-rule>").replace(work_dir, "<workdir>")
        if len(text) > _LINT_OUTPUT_CAP:
            text = text[:_LINT_OUTPUT_CAP] + "\n…(truncated)"
        if proc.returncode == 0:
            report = f"PASS — {binary} parsed the {kind} rule clean."
            if text:
                report += f"\nLinter notes:\n{text}"
            return (True, report)
        return (
            False,
            f"FAIL — {binary} rejected the {kind} rule (exit {proc.returncode}):\n"
            f"{text or '(no linter output)'}",
        )


def make_audit_tools(daemon: DaemonServices, owner: str) -> list:
    """Returns [read_evidence, prior_actions, rule_validate] in
    _BUS_TOOL_NAMES order."""

    @bus_tool(
        "rule_validate",
        "Lint a Sigma/YARA/Suricata rule DRAFT via the local linter CLI "
        "(`sigma check` / `yarac` / `suricata -T`). Returns PASS with any "
        "best-practice notes, or FAIL with the parse/compile errors — so "
        "you fix syntax BEFORE context_write'ing the rule for a downstream "
        "agent (an invalid rule wastes that agent's turn). `rule_kind` is "
        "one of 'sigma' / 'yara' / 'suricata'. `rule_text` is the full rule "
        "body (YAML for sigma, .yar source for yara, rules-format string "
        "for suricata). If a linter isn't installed on this host, the tool "
        "says so with the install command and tells you the rule wasn't "
        "checked (it may still be fine) — validate-then-publish either way "
        "so the order-of-ops is consistent.",
        _RuleValidateArgs,
    )
    async def rule_validate(args: dict[str, Any]) -> dict[str, Any]:
        kind = (args.get("rule_kind") or "").strip().lower()
        body = args.get("rule_text") or ""
        if kind not in _RULE_LINTERS:
            return _text(
                f"error: rule_kind must be 'sigma' / 'yara' / 'suricata'; got {kind!r}",
                error=True,
            )
        if not body or not body.strip():
            return _text("error: rule_text is required and must be non-empty", error=True)
        ok, report = await _run_rule_linter(kind, body)
        return _text(report, error=not ok)

    @bus_tool(
        "read_evidence",
        "Fetch a byte range from a truncated tool result stashed in the "
        "engagement evidence cache. When a tool returns a large output, "
        "the wrapper replaces the bulk of the content with a marker like "
        "'evidence_cache/abcdef123456.txt' — pass that 12-char sha here "
        "to retrieve more of it. Length is capped at 32 KB per call so "
        "you can scan a body in chunks instead of loading it all back "
        "into context. Only re-fetch when the head/tail returned inline "
        "wasn't enough to make the decision you need.",
        _ReadEvidenceArgs,
    )
    async def read_evidence(args: dict[str, Any]) -> dict[str, Any]:
        read_evidence_text = _skin_module("truncate").read_evidence_text

        sha = (args.get("sha") or "").strip()
        offset = args["offset"]  # model-validated int, ge=0
        length = args["length"]  # model-validated int, ge=1, le=32768
        ok, payload = read_evidence_text(
            engagement_path=daemon.engagement_path,
            sha=sha,
            offset=offset,
            length=length,
        )
        return _text(payload, error=not ok)

    @bus_tool(
        "prior_actions",
        "Look up what tool calls have already happened in this engagement. "
        "Use this BEFORE launching a tool against a target — if the same "
        "(tool, target) ran recently with a usable result, cite the outcome "
        "instead of repeating the work. The per-task Prior Actions block "
        "you receive at the top of a task is a snapshot; this tool fetches "
        "the live view with filters. All parameters are optional — call "
        "with `{}` to get every action in this engagement (capped by limit).\n"
        "  target          — substring of the canonical target_key "
        "('host:1.2.3.4', 'url:http://...', or just '1.2.3.4'). Optional.\n"
        "  tool            — substring of the tool name ('curl', 'grep'). "
        "Optional.\n"
        "  since_minutes   — only actions newer than N minutes. Optional.\n"
        "  limit           — max rows to return. Optional.\n"
        "  include_args    — if true, append the full args JSON for each "
        "row. Use this when you need to retry a failed call and the "
        "summary doesn't carry the original payload (host/port/body). "
        "Optional. CAUTION: original args may contain "
        "secrets (passwords, tokens, hashes) if a previous tool was "
        "called with them inline. Leave include_args off for general "
        "browsing; turn it on only for the specific row you intend to "
        "retry. The returned JSON lands in YOUR conversation context "
        "verbatim — no scrubbing.\n"
        "Returns one line per action: HH:MM agent tool target outcome "
        "summary. With include_args, each line is followed by an "
        "indented `args: {...}` block.",
        # Full JSON schema (not the SDK's `{name: type}` shorthand) so we can
        # explicitly declare `required: []`. The shorthand marks every key as
        # required, which broke small/external models that read the
        # description and called with only the params they wanted.
        _PriorActionsArgs,
    )
    async def prior_actions(args: dict[str, Any]) -> dict[str, Any]:
        target = args["target"].strip() or None
        tool_filter = args["tool"].strip() or None
        since_min = args["since_minutes"]  # None ⇒ unbounded; else int ≥ 1
        since_ts: float | None = None if since_min is None else time.time() - since_min * 60.0
        limit = args["limit"]  # model-validated int, ge=1
        include_args = args["include_args"]
        try:
            rows = daemon.actions.query(
                target=target,
                tool=tool_filter,
                since_ts=since_ts,
                limit=limit,
            )
        except Exception as e:  # noqa: BLE001
            return _text(f"prior_actions error: {type(e).__name__}: {e}", error=True)
        if not rows:
            return _text("(no matching actions in this engagement)")
        if not include_args:
            return _text("\n".join(a.to_line() for a in rows))
        parts: list[str] = []
        for a in rows:
            parts.append(a.to_line())
            parts.append(f"    args: {a.args_json}")
        return _text("\n".join(parts))

    return [read_evidence, prior_actions, rule_validate]
