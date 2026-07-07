"""Bus discovery tools — list_agents, search_skills, get_skill.

The "look something up" tools. Extracted from salient/bus.py during
the package split; @tool closure shape preserved verbatim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ._common import *  # noqa: F401,F403
from ._common import bus_tool

if TYPE_CHECKING:
    from ..protocols import DaemonServices


# Wire schemas. list_agents.filter is de-required — its description documents
# empty ⇒ all agents, a value worth defaulting to, so {} lists everything.
# search_skills.query / get_skill.name are genuinely required (no documented
# empty-default).
class _ListAgentsArgs(BaseModel):
    filter: str = Field("", description="empty ⇒ all agents; a substring narrows.")


class _SearchSkillsArgs(BaseModel):
    query: str


class _GetSkillArgs(BaseModel):
    name: str


def make_discovery_tools(daemon: DaemonServices, owner: str) -> list:
    """Returns [list_agents, search_skills, get_skill] in
    the order specified by _BUS_TOOL_NAMES."""

    @bus_tool(
        "list_agents",
        "List running agents you can ask_agent / context_read against. "
        "Pass empty filter for all, or a substring to narrow.",
        _ListAgentsArgs,
    )
    async def list_agents(args: dict[str, Any]) -> dict[str, Any]:
        from ..alias import rewrite_outbound as _alias_outbound
        from ..alias import to_real as _alias_to_real

        flt = args["filter"].strip().lower()  # default "" ⇒ no filter ⇒ all agents
        # Reverse-alias the filter so a wire-facing alias matches the
        # agent's real internal name.
        flt = _alias_to_real(flt).lower() if flt else flt
        rows = []
        for r in daemon.runners.values():
            if flt and flt not in r.name.lower():
                continue
            sub = (r.cfg or {}).get("substitute_for")
            sub_tag = f"  (substitutes_for={sub})" if sub else ""
            rows.append(
                f"{r.name}  status={r.status}  queued={r.queue.qsize()}  "
                f"completed={r.jobs_recorded}{sub_tag}"
            )
        return _text(_alias_outbound("\n".join(rows) if rows else "(no agents)"))

    @bus_tool(
        "search_skills",
        "Search the local skill library by keyword/tool/description. "
        "Returns a ranked list of skills (name + description). Use get_skill "
        "to fetch the full methodology by name.",
        _SearchSkillsArgs,
    )
    async def search_skills(args: dict[str, Any]) -> dict[str, Any]:
        query = (args.get("query") or "").strip()
        skills = getattr(daemon, "skills", {}) or {}
        _search = _skin_module("skills").search_skills

        hits = _search(skills, query, limit=10)
        if not hits:
            return _text("(no skills matched)")
        lines = []
        for s in hits:
            cat = f"[{s.category}] " if s.category else ""
            op = f"  opsec={s.opsec}" if s.opsec else ""
            lines.append(f"{cat}{s.name}  ({', '.join(s.tools) or '-'}){op}  — {s.description}")
        return _text("\n".join(lines))

    @bus_tool(
        "get_skill",
        "Fetch the full methodology body for a skill by name. "
        "Use search_skills first to find the right name.",
        _GetSkillArgs,
    )
    async def get_skill(args: dict[str, Any]) -> dict[str, Any]:
        name = (args.get("name") or "").strip()
        skills = getattr(daemon, "skills", {}) or {}
        s = skills.get(name)
        if s is None:
            return _text(f"(no skill named {name!r})", error=True)
        header = f"# {s.name}\n_{s.description}_\n"
        return _text(header + "\n" + s.body)

    return [list_agents, search_skills, get_skill]
