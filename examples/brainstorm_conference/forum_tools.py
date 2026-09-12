"""Bus tools that expose the conference forum to agents: ``forum_post`` /
``forum_read``.

Authored with the kernel's ``bus_tool`` decorator (the validating cousin of
``@tool`` — the wire schema is derived from the pydantic model and args are
validated before the handler runs). These are injected into every agent's bus
via the ``set_bus_builder`` seam in the app wiring, so both the moderator and
the debaters get ``forum_post`` / ``forum_read`` alongside the built-in bus
tools with no kernel edits.

The store itself (``ForumStore``) is created once per run and closed over here;
these wrappers are thin, validated adapters onto it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from salient_core.bus._common import _text, bus_tool

try:
    from forum import ForumStore
except ImportError:  # pragma: no cover - path shim, mirrors consensus_panel
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from forum import ForumStore


class _ForumPostArgs(BaseModel):
    text: str = Field(
        ...,
        min_length=1,
        description="Your contribution to the discussion thread — one clear point, "
        "argument, or response to what someone else said.",
    )
    round: int = Field(
        0,
        ge=0,
        description="Current discussion round; the moderator sets this. Use 0 if unsure.",
    )


class _ForumReadArgs(BaseModel):
    since: int = Field(
        0,
        ge=0,
        description="Return only posts after this seq number. 0 (default) ⇒ the whole "
        "thread so far. Pass the last seq you saw to get only what's new.",
    )


def make_forum_tools(store: ForumStore, owner: str) -> list:
    """Build ``[forum_post, forum_read]`` bound to ``store`` for agent ``owner``.

    ``owner`` is the posting agent's name — the wire caller never supplies it, so
    an agent cannot post as someone else.
    """

    @bus_tool(
        "forum_post",
        "Post a message to the shared conference thread that ALL participants read. "
        "Use it to state your argument, push back on someone, or build on an idea. "
        "Everyone sees every post — this is the round table, not a private note.",
        _ForumPostArgs,
    )
    async def forum_post(args: dict[str, Any]) -> dict[str, Any]:
        entry = store.post(owner, args["text"], round=args["round"])
        return _text(f"posted #{entry.seq} (round {entry.round})")

    @bus_tool(
        "forum_read",
        "Read the shared conference thread. Pass since=<last seq you saw> to get only "
        "new posts, or omit it to read the whole discussion so far. Read before you "
        "post so you're building on the argument, not repeating it.",
        _ForumReadArgs,
    )
    async def forum_read(args: dict[str, Any]) -> dict[str, Any]:
        return _text(store.transcript(since=args["since"]))

    return [forum_post, forum_read]
