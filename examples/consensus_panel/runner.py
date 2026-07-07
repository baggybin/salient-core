"""Panel runner for the consensus showcase — the engine behind the split panes.

A *panel run* fans one question to several legs (each a model), streams each
leg's reasoning trace and final answer, then scores convergence and renders a
judge verdict. The UI subscribes to the event stream and paints one pane per
leg plus a convergence panel.

Two things are real here, not mocked:

* **Convergence** is computed by the kernel's own
  :func:`salient_core.bus._consensus.semantic_agreement` — the same embedding-
  cosine measure ``ask_consensus`` uses — fed by a deterministic hash embedder
  so the demo scores meaningfully offline.
* **The event contract** (:class:`Event`) mirrors the ``ask_consensus`` payload
  (per-leg trace + answer, ``semantic_score``, judge). A real bus-backed runner
  is a drop-in: emit the same events from ``AgentRunner`` output instead of the
  canned generator below.

``MockPanelRunner`` generates deterministic answers/traces so the showcase runs
with no API key. Swap it for a bus-backed runner to drive live models.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

# Import the kernel's real convergence scorer. The example dir isn't a package,
# so make the src layout importable when this module is imported directly.
try:
    from salient_core.bus._consensus import semantic_agreement
    from salient_core.memory.embeddings import cosine
except ModuleNotFoundError:  # pragma: no cover - convenience for `python server.py`
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from salient_core.bus._consensus import semantic_agreement
    from salient_core.memory.embeddings import cosine


# ─── event contract (one shape for the mock and any real runner) ─────────────


@dataclass
class Event:
    """One SSE event. ``kind`` drives the UI; ``leg`` scopes leg-specific events."""

    kind: str  # run_start|leg_start|trace|answer|aborted|error|convergence|done
    leg: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


Emit = Callable[[Event], Awaitable[None]]


@dataclass
class Leg:
    id: str
    model: str


# ─── deterministic hash embedder (real semantic scoring, no network) ─────────


_WORD = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    """Bag-of-words vectors in a fixed-dim space via stable hashing. Similar
    answers point the same way, so the kernel's cosine-based
    ``semantic_agreement`` returns a meaningful score with no embedding API.
    Async ``embed`` matches the ``Embedder`` shape the scorer expects."""

    def __init__(self, dim: int = 96) -> None:
        self.dim = dim
        self.model = "hash-embedder-v1"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _WORD.findall((text or "").lower()):
            h = int(hashlib.sha1(tok.encode()).hexdigest(), 16) % self.dim
            v[h] += 1.0
        return v

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


# ─── canned per-model answers (deterministic, varied by model) ───────────────
#
# The demo ships ONE canned question (speeding up repeated calls); each model
# gives a slightly different take so convergence is neither 1.0 nor 0.0. Only
# this table is question-specific — the judge below scores whatever answers it
# is given, so swapping the canned content keeps the verdict honest.

_ANSWERS: dict[str, str] = {
    "a": (
        "Cache the computed result and reuse it: memoize the function so repeated "
        "calls with the same inputs return the stored value instead of recomputing. "
        "This trades a little memory for a large speedup on hot paths."
    ),
    "b": (
        "Memoization is the fix — store each result keyed by its arguments and "
        "return the cached value on repeat calls. It costs some memory but removes "
        "the redundant recomputation on frequently hit paths."
    ),
    "c": (
        "Parallelize the work across threads so independent calls run at once. "
        "Splitting the batch over a worker pool cuts wall-clock time when the "
        "calls don't depend on each other."
    ),
}

_TRACES: dict[str, list[tuple[str, str | None, str]]] = {
    "a": [
        ("thinking", None, "The bottleneck looks like repeated identical work."),
        ("tool_call", "profile", "profiled the hot path — 80% in one pure function"),
        ("thinking", None, "A pure function with repeat inputs is a caching candidate."),
    ],
    "b": [
        ("thinking", None, "Repeated calls with the same args — classic memoize case."),
        ("tool_call", "grep", "found the call site repeated in a loop"),
    ],
    "c": [
        ("thinking", None, "The calls look independent; could run them concurrently."),
        ("tool_call", "profile", "profiled — calls are independent, CPU-bound"),
        ("thinking", None, "A worker pool would parallelize the batch."),
    ],
}


def _answer_for(leg: Leg) -> str:
    return _ANSWERS.get(leg.id, _ANSWERS["a"])


def _trace_for(leg: Leg) -> list[tuple[str, str | None, str]]:
    return _TRACES.get(leg.id, _TRACES["a"])


# ─── the runner ──────────────────────────────────────────────────────────────


class MockPanelRunner:
    """Streams a deterministic panel run. Per-leg abort is honored between trace
    steps. Convergence is scored for real via the kernel's ``semantic_agreement``.

    ``step_delay`` paces the trace so the UI streams visibly; set 0 in tests.
    """

    def __init__(self, *, step_delay: float = 0.4) -> None:
        self.step_delay = step_delay
        self._embedder = HashEmbedder()

    async def run(
        self,
        question: str,
        panel: list[Leg],
        judge_model: str,
        emit: Emit,
        aborts: dict[str, asyncio.Event],
    ) -> None:
        await emit(
            Event("run_start", data={"question": question, "panel": [asdict(l) for l in panel]})
        )

        answers = await asyncio.gather(
            *[self._run_leg(leg, emit, aborts.get(leg.id)) for leg in panel]
        )
        ok = {leg.id: text for leg, text in zip(panel, answers, strict=True) if text is not None}

        # Real convergence: embedding cosine across the answers that finished.
        semantic = await semantic_agreement(ok, self._embedder)
        judge = await self._judge(question, ok, judge_model)
        await emit(
            Event(
                "convergence",
                data={
                    "semantic_score": round(semantic, 4) if semantic is not None else None,
                    "answered": list(ok),
                    "judge": judge,
                    "judge_model": judge_model,
                },
            )
        )
        await emit(Event("done"))

    async def _run_leg(self, leg: Leg, emit: Emit, abort: asyncio.Event | None) -> str | None:
        await emit(Event("leg_start", leg=leg.id, data={"model": leg.model}))
        for kind, tool, text in _trace_for(leg):
            if abort is not None and abort.is_set():
                await emit(Event("aborted", leg=leg.id))
                return None
            if self.step_delay:
                await asyncio.sleep(self.step_delay)
            await emit(Event("trace", leg=leg.id, data={"step": kind, "tool": tool, "text": text}))
        if abort is not None and abort.is_set():
            await emit(Event("aborted", leg=leg.id))
            return None
        if self.step_delay:
            await asyncio.sleep(self.step_delay)
        answer = _answer_for(leg)
        await emit(Event("answer", leg=leg.id, data={"text": answer}))
        return answer

    # Two answers whose bag-of-words cosine clears this are "the same fix".
    _JUDGE_AGREE_COSINE = 0.5

    async def _judge(self, question: str, answers: dict[str, str], judge_model: str) -> str | None:
        """A deterministic stand-in for the LLM judge, content-agnostic: it
        thresholds the same embedding cosine the convergence score is built
        from, so the verdict stays honest if the canned question/answers
        change. A real runner routes this to ``ask_consensus``'s judge_agent
        on ``judge_model``."""
        legs = list(answers)
        if len(legs) < 2:
            return None
        embedded = await self._embedder.embed([answers[leg] for leg in legs])
        vecs = dict(zip(legs, embedded, strict=True))
        pair_score = {
            (a, b): cosine(vecs[a], vecs[b]) for i, a in enumerate(legs) for b in legs[i + 1 :]
        }
        (a, b), best = max(pair_score.items(), key=lambda kv: kv[1])
        if best < self._JUDGE_AGREE_COSINE:
            return (
                f"DIVERGE: the closest pair ({a}, {b}, cosine {best:.2f}) still propose "
                f"materially different fixes. Weigh them by which bottleneck the "
                f"evidence actually shows."
            )

        def _pair(x: str, y: str) -> float:
            return pair_score.get((x, y), pair_score.get((y, x), 0.0))

        outliers = [
            leg
            for leg in legs
            if leg not in (a, b) and max(_pair(leg, a), _pair(leg, b)) < self._JUDGE_AGREE_COSINE
        ]
        verdict = f"AGREE (cosine {best:.2f}): {a} and {b} converge on the same fix."
        if outliers:
            verdict += (
                f" {', '.join(outliers)} proposes a different approach — credible, but "
                f"judge it against the bottleneck the evidence actually shows."
            )
        return verdict
