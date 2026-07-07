"""Bus tool: propose_skill — agent-proposed methodology playbooks, operator-gated.

The skill library (salient/skills.py) is read-only for agents — search_skills /
get_skill fetch playbooks the operator curates. This tool adds the WRITE side:
an agent PROPOSES a new playbook; nothing reaches the live library without
operator approval.

NON-BLOCKING (the key divergence from propose_lesson): the proposal is queued
for review and the agent returns immediately — it does NOT wait on the operator.
The draft lands in skills/pending/ and pings every operator inbox; the operator
clears the queue at leisure (`salientctl skills approve <id>`). Proposals are
secret-redacted, length-capped, and rate-limited per (engagement, agent) so a
confused/compromised agent can't spam the queue or leak secrets onto disk.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ._common import *  # noqa: F401,F403  -- _text
from ._common import _SECRET_PATTERNS, _redact_operator_infra, _skin_module, bus_tool

if TYPE_CHECKING:
    from ..protocols import DaemonServices


class _ProposeSkillArgs(BaseModel):
    name: str
    description: str
    body: str
    # Neutral empty-list defaults (per pattern, no field description needed);
    # default_factory gives each call its own list.
    keywords: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


_MAX_DESC = 200
_MAX_BODY = 16_000
_MAX_PROPOSALS = 5
# Per (engagement_id, agent) proposal counter — best-effort abuse cap for the
# daemon's lifetime. Resets on restart, which is fine for rate-limiting.
_SKILL_PROPOSAL_COUNTS: dict[tuple[str | None, str], int] = {}


def _redact(text: str) -> str:
    for _label, pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def make_skills_tools(daemon: DaemonServices, owner: str) -> list:
    @bus_tool(
        "propose_skill",
        "Propose a NEW reusable SKILL — a methodology PLAYBOOK (markdown), not "
        "code/tools/agents — for the shared skill library every agent reads via "
        "search_skills / get_skill. Use it ONLY when you've worked out a "
        "repeatable, team-worthy methodology worth keeping across engagements "
        "(a one-off note for your future self is propose_lesson; a finding is "
        "context_write). NON-BLOCKING: this queues the playbook for operator "
        "review and returns immediately — keep working. Nothing is written "
        "without operator approval; on approval it goes live for all agents "
        "with no reset.\n"
        "  name        — REQUIRED. kebab-case id, unique (e.g. 'api-retry-backoff').\n"
        "  description — REQUIRED. one line: what it's for.\n"
        "  body        — REQUIRED. the full methodology, markdown.\n"
        "  keywords    — Optional. list of search terms.\n"
        "  tools       — Optional. list of tool names it uses.",
        _ProposeSkillArgs,
    )
    async def propose_skill(args: dict[str, Any]) -> dict[str, Any]:
        name = (args.get("name") or "").strip()
        description = (args.get("description") or "").strip()
        body = (args.get("body") or "").strip()
        if not name:
            return _text("error: 'name' is required", error=True)
        try:
            _skin_module("skills").validate_name(name)
        except ValueError as e:
            return _text(f"error: {e}", error=True)
        if not description:
            return _text("error: 'description' is required", error=True)
        if len(description) > _MAX_DESC:
            return _text(
                f"error: description too long (>{_MAX_DESC} chars) — keep it to one line",
                error=True,
            )
        if not body:
            return _text("error: 'body' is required (the methodology)", error=True)
        if len(body) > _MAX_BODY:
            return _text(
                f"error: body too long (>{_MAX_BODY} chars) — tighten the playbook or split it",
                error=True,
            )
        keywords = [str(k).strip() for k in args["keywords"] if str(k).strip()]
        tools = [str(t).strip() for t in args["tools"] if str(t).strip()]

        eng = daemon.engagement_path.name if daemon.engagement_path is not None else None
        key = (eng, owner)
        if _SKILL_PROPOSAL_COUNTS.get(key, 0) >= _MAX_PROPOSALS:
            return _text(
                f"error: skill-proposal limit reached ({_MAX_PROPOSALS} this "
                "engagement) — ask the operator to add further skills directly",
                error=True,
            )

        # Redact secrets + operator infra (LHOST/LPORT/local IPs) BEFORE anything
        # touches disk — a proposed playbook must not persist either.
        clean_desc = _redact(description)
        clean_body = _redact(body)
        with suppress(Exception):
            clean_desc, _ = _redact_operator_infra(clean_desc, daemon)
        with suppress(Exception):
            clean_body, _ = _redact_operator_infra(clean_body, daemon)

        spec = {
            "name": name,
            "description": clean_desc,
            "body": clean_body,
            "keywords": keywords,
            "tools": tools,
        }
        try:
            pid = daemon.add_skill_proposal(owner, spec)
        except ValueError as e:
            # Name collision / invalid name — a real rejection, don't burn quota.
            return _text(f"error: {e}", error=True)
        except Exception as e:  # noqa: BLE001
            return _text(f"propose_skill error: {type(e).__name__}: {e}", error=True)
        # Count only AFTER the proposal is successfully filed — a transient
        # filing error shouldn't burn the agent's quota.
        _SKILL_PROPOSAL_COUNTS[key] = _SKILL_PROPOSAL_COUNTS.get(key, 0) + 1
        return _text(
            f"queued for operator review (id {pid}) — keep working. If approved "
            "it goes live for all agents (search_skills / get_skill) with no reset."
        )

    return [propose_skill]
