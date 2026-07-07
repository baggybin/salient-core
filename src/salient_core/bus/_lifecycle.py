"""Bus lifecycle / control-plane tools — spawn_template, swarm_finish.

Create child agents from templates; tear down swarms. Extracted from
salient/bus.py during the package split.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ._common import *  # noqa: F401,F403
from ._common import bus_tool

if TYPE_CHECKING:
    from ..protocols import DaemonServices


# spawn_template.name is genuinely required and errors on empty (no fallback).
# swarm_finish.reason is DE-REQUIRED (str="") to keep this migration behavior-
# preserving: the old @tool path never validated args, so an OMITTED reason
# reached the handler and coalesced to the "swarm_finish (explicit)" placeholder
# — teardown ran. Keeping reason required would make @bus_tool reject an omitted
# reason BEFORE the handler, blocking teardown over a missing label — the worst
# trade for a cleanup tool (a sloppy/small model that omits reason would leave
# the swarm running). "" still flows to the handler's placeholder, so the reason
# is recorded to teardown logs/findings; the model just isn't forced to supply
# one to tear down. A whitespace-only reason is likewise absorbed by the handler.
class _SpawnTemplateArgs(BaseModel):
    name: str


class _SwarmFinishArgs(BaseModel):
    reason: str = ""


def make_lifecycle_tools(daemon: DaemonServices, owner: str) -> list:
    """Returns [spawn_template, swarm_finish] in _BUS_TOOL_NAMES order."""

    @bus_tool(
        "spawn_template",
        "Spawn an agent from a shipped YAML template (templates/<name>.yaml) "
        "and start it. The classic use case: an orchestrator (manager, "
        "sim_loop) needs a `planner` or `reviewer` running but the operator "
        "hasn't spawned them yet — instead of filing an ask_operator "
        "question and waiting, call this and have the lead ready in seconds.\n"
        "  name — template name (without .yaml). Sanitized to "
        "[A-Za-z0-9_-]+ to prevent path traversal. Common values: "
        "'planner', 'reviewer'. The spawned agent takes the name in the "
        "template's `name:` field, not necessarily the template filename.\n"
        "Idempotent for already-running spawns — returns {ok:true, "
        "already_running:true} rather than erroring.\n"
        "RESTRICTED: bus_trusted callers only (manager / sim_loop / "
        "sherlock today). Non-bus_trusted callers should use <ask_operator> "
        "to request the operator run `salientctl spawn templates/<n>.yaml`.",
        _SpawnTemplateArgs,
    )
    async def spawn_template(args: dict[str, Any]) -> dict[str, Any]:
        import re as _re
        from pathlib import Path as _Path

        import yaml as _yaml

        # Trust check — only bus_trusted callers spawn.
        _caller_runner = daemon.runners.get(owner)
        caller_cfg = (
            daemon.all_cfgs.get(owner) or (_caller_runner.cfg if _caller_runner else None) or {}
        )
        if not caller_cfg.get("bus_trusted"):
            return _text(
                f"spawn_template refused: caller {owner!r} is not "
                f"bus_trusted. Use <ask_operator> to request the operator "
                f"run `salientctl spawn templates/<name>.yaml`.",
                error=True,
            )

        raw_name = (args.get("name") or "").strip()
        if not raw_name:
            return _text("spawn_template: 'name' is required", error=True)
        if not _re.match(r"^[A-Za-z0-9_-]+$", raw_name):
            return _text(
                f"spawn_template: 'name' must be [A-Za-z0-9_-]+ (got {raw_name!r})",
                error=True,
            )

        # templates/ lives at the project root (cwd of the daemon process).
        template_path = _Path("templates") / f"{raw_name}.yaml"
        if not template_path.exists():
            return _text(
                f"spawn_template: no template at {template_path!s}. "
                f"Available templates: "
                f"{[p.stem for p in _Path('templates').glob('*.yaml')]}",
                error=True,
            )

        try:
            doc = _yaml.safe_load(template_path.read_text())
        except Exception as e:  # noqa: BLE001
            return _text(
                f"spawn_template: failed to parse {template_path!s}: {type(e).__name__}: {e}",
                error=True,
            )

        # Templates can be a bare agent dict, a list, or wrapped in
        # {agents: [...]} — same defensive unwrap as _cmd_spawn does.
        if isinstance(doc, list):
            cfg = doc[0] if doc else {}
        elif isinstance(doc, dict) and "agents" in doc and not doc.get("name"):
            cfg = (doc["agents"] or [{}])[0]
        else:
            cfg = doc
        if not isinstance(cfg, dict) or not cfg.get("name"):
            return _text(
                f"spawn_template: template {template_path!s} has no "
                f"agent config with a 'name' field",
                error=True,
            )

        spawned_name = cfg["name"]
        existing = daemon.runners.get(spawned_name)
        if existing is not None and existing.status not in ("stopped",):
            return _text(
                f"spawn_template: {spawned_name!r} already running "
                f"(status={existing.status!r}); reuse it via "
                f"ask_agent({spawned_name!r}, ...)"
            )

        # Use the same code path as the spawn RPC.
        try:
            runner = daemon._make_runner(cfg)
            daemon.runners[runner.name] = runner
            await runner.start()
            daemon._notify_agent_spawn(runner.name, cfg, runner)
            daemon._persist_running_agents()
        except Exception as e:  # noqa: BLE001
            return _text(
                f"spawn_template: failed to start {spawned_name!r}: {type(e).__name__}: {e}",
                error=True,
            )
        return _text(
            f"spawned {spawned_name!r} from templates/{raw_name}.yaml. "
            f"Call it now via ask_agent({spawned_name!r}, ...)."
        )

    @bus_tool(
        "swarm_finish",
        "End this SWARM: persist findings + synthesis and tear down "
        "the orchestrator + all members. Only callable by SWARM "
        "orchestrators. Use this when the swarm's task is complete and "
        "you don't want to wait for auto-teardown on job-complete (or "
        "when the swarm is non-ephemeral but the task is done).",
        _SwarmFinishArgs,
    )
    async def swarm_finish(args: dict[str, Any]) -> dict[str, Any]:
        # Gate: this tool is only meaningful for swarm orchestrators.
        # Non-orchestrators trying to call it get a clear refusal so
        # the operator can see the mis-use in logs.
        swarms = getattr(daemon, "_swarms", {}) or {}
        if owner not in swarms:
            return _text(
                f"error: swarm_finish is only callable by SWARM "
                f"orchestrators; {owner!r} is not one. Use this tool "
                f"only inside an orchestrator agent spawned via "
                f"swarm_create.",
                error=True,
            )
        reason = (args.get("reason") or "").strip() or "swarm_finish (explicit)"
        # Schedule the teardown so the current tool-call return path
        # completes cleanly first — calling _swarm_teardown inline
        # would race the runner's own stop() on this agent.
        asyncio.create_task(daemon._swarm_teardown(owner, reason=reason))
        members = list(swarms[owner].get("members") or [])
        return _text(
            f"swarm_finish scheduled: tearing down {owner!r} and "
            f"{len(members)} members ({', '.join(members) or '(none)'}). "
            f"Findings + synthesis will persist to context_write + the "
            f"engagement directory (when configured). End your turn — "
            f"the runners are about to stop."
        )

    return [spawn_template, swarm_finish]
