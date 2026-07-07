"""Bus tool: propose_lesson — agent-proposed procedural memory, operator-gated.

Lessons are operator-curated by design (lessons.py: the agent READS, the operator
WRITES). This tool lets an agent PROPOSE a durable lesson for its future self;
nothing reaches disk without an explicit operator approve/edit via the
QuestionInbox gate. Proposals are secret-redacted, length-capped, source-tagged,
and rate-limited per (engagement, agent) so a confused/compromised agent can't
poison its own future system prompt or spam the operator inbox.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from ..memory import lessons as _lessons
from ._common import *  # noqa: F401,F403  -- _text
from ._common import _SECRET_PATTERNS, _parse_delegation_answer, _redact_operator_infra, bus_tool

if TYPE_CHECKING:
    from ..protocols import DaemonServices


class _ProposeLessonArgs(BaseModel):
    text: str
    kind: str = Field("tactic", description="tactic | gotcha | workaround; defaults to 'tactic'.")

    @field_validator("kind")
    @classmethod
    def _blank_kind_is_tactic(cls, v: str) -> str:
        # Single source of truth for the strip + fallback the handler used to do.
        return v.strip() or "tactic"


_MAX_LEN = 500
_MAX_PROPOSALS = 10
# Per (engagement_id, agent) proposal counter — best-effort abuse cap for the
# daemon's lifetime. Resets on restart, which is fine for rate-limiting.
_PROPOSAL_COUNTS: dict[tuple[str | None, str], int] = {}


def _redact(text: str) -> str:
    for _label, pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def make_lessons_tools(daemon: DaemonServices, owner: str) -> list:
    @bus_tool(
        "propose_lesson",
        "Propose a durable LESSON for your future self (cross-engagement "
        "procedural memory) — a tactic, gotcha, or workaround worth keeping. "
        "The operator must APPROVE before it is saved; nothing is written on "
        "deny/timeout. One actionable sentence (<500 chars). Takes effect on "
        "your next reset.\n"
        "  text — REQUIRED. The lesson, one line.\n"
        "  kind — Optional. tactic | gotcha | workaround.",
        _ProposeLessonArgs,
    )
    async def propose_lesson(args: dict[str, Any]) -> dict[str, Any]:
        text = (args.get("text") or "").strip()
        if not text:
            return _text("error: 'text' is required", error=True)
        if len(text) > _MAX_LEN:
            return _text(
                f"error: lesson too long (>{_MAX_LEN} chars) — tighten it to "
                "one actionable sentence",
                error=True,
            )
        kind = args["kind"]  # model-normalized (strip + fallback to 'tactic')
        eng = daemon.engagement_path.name if daemon.engagement_path is not None else None
        key = (eng, owner)
        if _PROPOSAL_COUNTS.get(key, 0) >= _MAX_PROPOSALS:
            return _text(
                f"error: lesson-proposal limit reached ({_MAX_PROPOSALS} this "
                "engagement) — ask the operator to add further lessons directly",
                error=True,
            )
        clean = _redact(text)
        # Also strip operator-side infra (LHOST/LPORT/local IPs) so a proposed
        # lesson can't persist them; placeholders are fine in stored prose.
        with suppress(Exception):
            clean, _infra = _redact_operator_infra(clean, daemon)
        try:
            qid, fut = daemon.add_lesson_proposal_question(owner, clean, kind)
        except Exception as e:  # noqa: BLE001
            return _text(f"propose_lesson error: {type(e).__name__}: {e}", error=True)
        # Count only AFTER the proposal is successfully filed — a transient
        # filing error shouldn't burn the agent's quota. (deny/timeout DO
        # count: they consumed a real operator interaction.)
        _PROPOSAL_COUNTS[key] = _PROPOSAL_COUNTS.get(key, 0) + 1
        try:
            answer = await asyncio.wait_for(fut, timeout=600.0)
        except TimeoutError:
            with suppress(Exception):
                daemon.inbox.expire(qid, "[timed out]")
            return _text("operator did not respond in time; lesson NOT saved")
        verdict, payload = _parse_delegation_answer(answer)
        if verdict == "approve":
            _lessons.append(owner, f"[proposed by {owner}, {kind}] {clean}")
            return _text("operator approved — lesson saved (takes effect on your next reset)")
        if verdict == "edit":
            edited = _redact((payload or "").strip())
            if not edited:
                return _text("operator sent an empty edit; lesson NOT saved")
            _lessons.append(owner, f"[proposed by {owner}, {kind}, operator-edited] {edited}")
            return _text(
                "operator approved with edits — lesson saved (takes effect on your next reset)"
            )
        return _text(f"operator declined — lesson NOT saved ({payload or 'no reason given'})")

    return [propose_lesson]
