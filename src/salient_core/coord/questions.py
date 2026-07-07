"""Question inbox: tracks operator-targeted questions filed by agents.

A `Question` is filed when an agent either calls the `ask_operator` MCP tool
(`add`) or embeds a `<ask_operator>...</ask_operator>` marker that the daemon
strips and lifts out of the reply (also `add`). The inbox does not know about
runners or stdout; the daemon coordinates announcements after calling `add`.

Subscribers receive a snapshot of pending questions on connect, then live
'new'/'answered' events.
"""

from __future__ import annotations

import asyncio
import builtins
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


@dataclass
class Question:
    id: int
    agent: str
    job_id: int
    text: str
    ts: float
    # "operator" (default) — the agent paused its turn and the operator's reply
    # becomes the agent's next prompt. "delegation" — filed by ask_agent inside
    # the bus; the caller is awaiting the answer inside an in-flight tool call,
    # so the daemon resolves a future instead of queueing a prompt.
    kind: str = "operator"
    answered: bool = False
    answered_with: str | None = None
    answer_job_id: int | None = None
    # WHO answered (operator identity) — multi-operator attribution. None
    # for questions answered before the feature, or answered by a
    # trusted-local operator that declared no identity.
    answered_by: str | None = None
    # Target operator for a `kind="note"` operator-to-operator note (None =
    # broadcast to everyone). Unused for agent questions.
    to_operator: str | None = None


class QuestionInbox:
    # Cap on ANSWERED questions retained in memory. Without this the list grows
    # for the daemon's whole lifetime (every resolved question stays resident).
    # Pending/unanswered questions are NEVER pruned — they back delegation-gate
    # futures and per-agent pending counts. The store (when present) keeps the
    # full record for audit; we retain the most-recent answered so `questions
    # clear` phantom cleanup, list(include_answered=True), and get() on a
    # recently-answered qid still work.
    DEFAULT_MAX_ANSWERED = 500

    def __init__(
        self,
        store: Any | None = None,
        max_answered: int | None = None,
    ) -> None:
        self.questions: list[Question] = []
        self._next_id = 1
        self._subs: list[asyncio.Queue] = []
        # qid → future awaited by an in-flight tool call (delegation gates).
        self._pending: dict[int, asyncio.Future] = {}
        # Optional ContextStore for persistence — when present, every
        # add() / mark_answered() also writes through to the questions
        # table so a daemon restart doesn't lose pending operator
        # questions or strand agents waiting for an answer.
        self._store = store
        self._max_answered = max_answered if max_answered is not None else self.DEFAULT_MAX_ANSWERED

    def hydrate(self) -> int:
        """Load pending questions from the store into the inbox at
        daemon startup. Returns the number hydrated. Already-answered
        questions stay in the DB but are not re-loaded — keeps the
        in-memory list lean."""
        if self._store is None:
            return 0
        rows = self._store.load_pending_questions()
        for r in rows:
            self.questions.append(
                Question(
                    id=int(r["id"]),
                    agent=r["agent"],
                    job_id=int(r["job_id"]),
                    text=r["text"],
                    ts=float(r["asked_at"]),
                    kind=r["kind"],
                )
            )
        # Continue numbering past the highest id ever assigned (including
        # answered questions still in the DB), not just the highest in
        # the in-memory list — otherwise a fresh restart could re-issue
        # ids that already exist on disk.
        self._next_id = max(self._next_id, self._store.max_question_id() + 1)
        return len(rows)

    def add(self, agent: str, text: str, job_id: int = 0, kind: str = "operator") -> Question:
        """File a question. Returns the new record. Does not announce — the
        caller routes the banner / runner-stream / sub-fanout itself."""
        q = Question(
            id=self._next_id,
            agent=agent,
            job_id=job_id,
            text=text.strip(),
            ts=time.time(),
            kind=kind,
        )
        self._next_id += 1
        self.questions.append(q)
        if self._store is not None:
            self._store.record_question(
                qid=q.id,
                agent=q.agent,
                text=q.text,
                job_id=q.job_id,
                kind=q.kind,
                asked_at=q.ts,
            )
        return q

    def add_note(
        self,
        from_op: str,
        text: str,
        to_op: str | None = None,
    ) -> Question:
        """File an operator-to-operator coordination note. EPHEMERAL — not
        persisted (transient chat for the daemon session). kind='note',
        agent='operator:<from>'. Rides the same subscriber broadcast as
        questions so every connected operator sees it in the Q-inbox."""
        q = Question(
            id=self._next_id,
            agent=f"operator:{from_op}",
            job_id=0,
            text=text.strip(),
            ts=time.time(),
            kind="note",
            to_operator=to_op,
        )
        self._next_id += 1
        self.questions.append(q)
        return q

    def add_suggestion(self, source: str, text: str) -> Question:
        """File a copilot suggestion (assessment idea 4.11). EPHEMERAL — not
        persisted (advisory + potentially chatty; must not survive a restart
        or bloat the questions table). kind='suggestion', agent=<source>.

        Rides the same subscriber broadcast as questions/notes so every
        connected operator surface sees it, but operator surfaces branch on
        kind=='suggestion' to render/count it as ADVISORY (dismissable, never
        blocking) — it must NEVER inflate the blocking-question signal, and it
        is inert to every delegation/approval gate (see pending_for)."""
        q = Question(
            id=self._next_id,
            agent=source,
            job_id=0,
            text=text.strip(),
            ts=time.time(),
            kind="suggestion",
        )
        self._next_id += 1
        self.questions.append(q)
        return q

    def register_pending(self, qid: int, fut: asyncio.Future) -> None:
        """Attach a future to a question; resolved by mark_answered when the
        operator replies. Used for delegation gates (and any future tool-call
        gate that needs to block until the operator answers)."""
        self._pending[qid] = fut

    def pop_pending(self, qid: int) -> asyncio.Future | None:
        return self._pending.pop(qid, None)

    def get(self, qid: int | None) -> Question | None:
        if qid is None:
            return None
        return next((q for q in self.questions if q.id == qid), None)

    def _prune_answered(self) -> None:
        """Drop the oldest answered questions beyond the retention cap, keeping
        all pending/unanswered (they back futures + pending counts) and the
        most-recent answered. In-memory only — the store keeps the full record.
        Called after every resolution path (mark_answered / clear)."""
        answered = [q for q in self.questions if q.answered]
        excess = len(answered) - self._max_answered
        if excess <= 0:
            return
        drop = {id(q) for q in answered[:excess]}  # oldest-answered, insertion order
        self.questions = [q for q in self.questions if id(q) not in drop]

    def pending_for(self, agent: str) -> list[Question]:
        # Suggestions (assessment idea 4.11) carry a real agent name as their
        # source but are advisory — they never block the agent and it owes no
        # answer. Exclude them so they don't inflate per-agent pending counts
        # or keep an ephemeral swarm alive (callers: status/sitrep counts,
        # swarm-teardown "does this agent owe an answer?" checks).
        return [
            q
            for q in self.questions
            if not q.answered and q.agent == agent and q.kind != "suggestion"
        ]

    def list(self, *, include_answered: bool = False) -> list[Question]:
        if include_answered:
            return list(self.questions)
        return [q for q in self.questions if not q.answered]

    def clear(
        self,
        qids: builtins.list[int],
        *,
        reason: str = "cleared by operator",
    ) -> builtins.list[Question]:
        """Mark each given question answered with `reason` as the answer
        text. Persists via the store and is otherwise identical to
        `mark_answered` minus the answer_job_id (set to 0 — there's no
        actual reply job).

        Use case: post-restart cleanup when an agent that filed the
        question is no longer running (orphaned), or when the operator
        wants to dismiss a stale prompt without typing a real answer.
        Caller is responsible for resolving any pending delegation
        futures via `pop_pending` BEFORE calling this — clear() does
        not touch futures so the caller can choose deny / cancel /
        custom-text semantics."""
        cleared: list[Question] = []
        for qid in qids:
            q = self.get(qid)
            if q is None or q.answered:
                continue
            q.answered = True
            q.answered_with = reason
            q.answer_job_id = 0
            if self._store is not None:
                self._store.mark_question_answered(
                    qid=q.id,
                    answer=reason,
                    answer_job_id=0,
                )
            cleared.append(q)
        self._prune_answered()
        return cleared

    def expire(self, qid: int, reason: str = "[timed out]") -> Question | None:
        """Resolve a gate question the operator never answered (timeout or
        bus-cancel): drop the pending future, mark it answered with `reason`,
        and PUBLISH the 'answered' event so every operator surface drops it
        from the inbox immediately.

        Without the publish the question lingers as a phantom that
        `questions clear` then refuses to remove (it's already answered) —
        undeletable. Idempotent: a second call no-ops because mark_answered
        returns None once the question is answered, so 'answered' fires at
        most once per question. Mirrors the mark+publish that
        `_clear_stall_question` already does for bus-stall questions."""
        self.pop_pending(qid)
        q = self.mark_answered(qid, reason, 0)
        if q is not None:
            self.publish("answered", q)
        return q

    def mark_answered(
        self,
        qid: int,
        text: str,
        answer_job_id: int,
        answered_by: str | None = None,
    ) -> Question | None:
        q = self.get(qid)
        if q is None or q.answered:
            return None
        q.answered = True
        q.answered_with = text
        q.answer_job_id = answer_job_id
        q.answered_by = answered_by
        if self._store is not None:
            self._store.mark_question_answered(
                qid=qid,
                answer=text,
                answer_job_id=answer_job_id,
                answered_by=answered_by,
            )
        self._prune_answered()
        return q

    def to_payload(self, q: Question) -> dict[str, Any]:
        return {
            "id": q.id,
            "agent": q.agent,
            "job_id": q.job_id,
            "text": q.text,
            "ts": q.ts,
            "kind": q.kind,
            "answered": q.answered,
            "answered_with": q.answered_with,
            "answered_by": q.answered_by,
            "to_operator": q.to_operator,
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with suppress(ValueError):
            self._subs.remove(q)

    def publish(self, event: str, q: Question) -> None:
        evt = {
            "event": event,
            "question": self.to_payload(q),
            "ts": time.time(),
        }
        for sub in list(self._subs):
            try:
                sub.put_nowait(evt)
            except asyncio.QueueFull:
                pass
