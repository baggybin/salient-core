"""salient_core.daemon — agent runner, event hub, and task spawning.

Public API re-exports for convenience. Import from the submodule for
finer control.
"""

from __future__ import annotations

from ..protocols import DaemonServices
from ._event_hub import EventHub, _EventObservationMixin
from ._helpers import Job, _wrap_context_value
from ._prompts import set_prompts_root, set_thinking_provider
from ._runner_factory import set_spawn_observer
from ._tasks import join_background_tasks, spawn_background, track_background
from ._tool_registry import (
    get_kg_builder,
    get_subagent_builder,
    get_tool_builder,
    get_tool_bundle_builder,
    set_daemon_skin_modules,
    set_kg_builder,
    set_tool_builder,
    set_tool_bundle_builder,
    set_tool_wire_names,
)
from .runner import AgentRunner

__all__ = [
    "AgentRunner",
    "DaemonServices",
    "EventHub",
    "Job",
    "_EventObservationMixin",
    "_wrap_context_value",
    "get_kg_builder",
    "get_subagent_builder",
    "get_tool_bundle_builder",
    "get_tool_builder",
    "set_daemon_skin_modules",
    "set_kg_builder",
    "set_prompts_root",
    "set_spawn_observer",
    "set_thinking_provider",
    "set_tool_builder",
    "set_tool_bundle_builder",
    "set_tool_wire_names",
    "join_background_tasks",
    "spawn_background",
    "track_background",
]
