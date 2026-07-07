"""Consensus-panel demo server (Starlette) — the bus showcase's web surface.

Endpoints:
  GET  /                         → the split-pane UI
  GET  /api/models               → picker choices (live Anthropic API or fallback)
  POST /api/consensus            → start a run; returns {run_id}
  GET  /api/consensus/{id}/events → SSE stream of the run's events
  POST /api/consensus/{id}/abort → {"leg": "..."} cancels one leg mid-run

Runs are held in memory: each fans :class:`Event`s out to one queue per SSE
subscriber (with a bounded history replayed to late joiners), so many browser
tabs can watch the same run independently. Finished runs are evicted once the
table exceeds ``_RUNS_CAP``. The default backend is :class:`MockPanelRunner`
(offline, deterministic); point ``RUNNER`` at a bus-backed runner to drive
live models.

Run it:  uvicorn server:app --reload   (from this directory)
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import asdict
from pathlib import Path

from models import list_models
from runner import Event, Leg, MockPanelRunner
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

_HERE = Path(__file__).resolve().parent
_WEB = _HERE / "web"

RUNNER = MockPanelRunner()

# Keep at most this many events per run for late-joining tabs; a run past the
# cap replays a truncated head (the end sentinel is always delivered).
_HISTORY_CAP = 500
# Finished runs beyond this count are evicted (oldest first) on each start.
_RUNS_CAP = 50


class Run:
    """One in-flight panel run: per-subscriber event fan-out plus per-leg
    abort switches. Each SSE subscriber gets its own queue, pre-loaded with
    the run's history, so multiple tabs can watch the same run without
    stealing events from one another."""

    def __init__(self, question: str, panel: list[Leg], judge_model: str) -> None:
        self.question = question
        self.panel = panel
        self.judge_model = judge_model
        self._history: list[Event | None] = []
        self._subscribers: list[asyncio.Queue[Event | None]] = []
        self.aborts: dict[str, asyncio.Event] = {leg.id: asyncio.Event() for leg in panel}
        self.task: asyncio.Task | None = None

    @property
    def done(self) -> bool:
        return self.task is not None and self.task.done()

    def subscribe(self) -> asyncio.Queue[Event | None]:
        """A fresh queue pre-loaded with everything emitted so far."""
        q: asyncio.Queue[Event | None] = asyncio.Queue()
        for e in self._history:
            q.put_nowait(e)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event | None]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _emit(self, event: Event | None) -> None:
        # The None sentinel always lands in history so late subscribers end
        # cleanly; ordinary events stop accumulating past the cap.
        if event is None or len(self._history) < _HISTORY_CAP:
            self._history.append(event)
        for q in list(self._subscribers):  # snapshot: unsubscribe-safe
            await q.put(event)

    async def drive(self) -> None:
        try:
            await RUNNER.run(self.question, self.panel, self.judge_model, self._emit, self.aborts)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the server
            await self._emit(Event("error", data={"message": str(exc)}))
        finally:
            await self._emit(None)  # sentinel: stream complete


_RUNS: dict[str, Run] = {}
# Monotonic id source: never reused, so eviction can't mint a colliding id
# that clobbers a still-referenced run.
_RUN_SEQ = itertools.count(1)


def _evict_finished_runs() -> None:
    """Drop the oldest FINISHED runs until the table is back under the cap;
    in-flight runs are never evicted."""
    if len(_RUNS) <= _RUNS_CAP:
        return
    for run_id in [rid for rid, run in _RUNS.items() if run.done]:
        del _RUNS[run_id]
        if len(_RUNS) <= _RUNS_CAP:
            return


async def homepage(_: Request) -> FileResponse:
    return FileResponse(_WEB / "index.html")


async def api_models(_: Request) -> JSONResponse:
    # list_models() is synchronous (Anthropic SDK) and hits the network on its
    # first, uncached call — keep it off the event loop.
    models = await asyncio.to_thread(list_models)
    return JSONResponse({"models": [asdict(m) for m in models]})


async def api_start(request: Request) -> JSONResponse:
    body = await request.json()
    question = (body.get("question") or "").strip()
    panel_in = body.get("panel") or []
    judge_model = (body.get("judge_model") or "").strip()
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)
    panel = [
        Leg(id=str(p.get("id") or f"leg{i}"), model=str(p.get("model") or "").strip())
        for i, p in enumerate(panel_in)
        if p.get("model")
    ]
    if len(panel) < 2:
        return JSONResponse({"error": "pick at least 2 models for a panel"}, status_code=400)

    _evict_finished_runs()
    run_id = f"run-{next(_RUN_SEQ)}"
    run = Run(question, panel, judge_model or panel[0].model)
    _RUNS[run_id] = run
    run.task = asyncio.create_task(run.drive())
    return JSONResponse({"run_id": run_id, "panel": [asdict(l) for l in panel]})


async def api_events(request: Request) -> StreamingResponse:
    run_id = request.path_params["run_id"]
    run = _RUNS.get(run_id)
    if run is None:
        return JSONResponse({"error": "unknown run_id"}, status_code=404)

    async def gen():
        q = run.subscribe()
        try:
            while True:
                event = await q.get()
                if event is None:
                    yield "event: end\ndata: {}\n\n"
                    return
                payload = json.dumps(event.as_dict())
                yield f"data: {payload}\n\n"
        finally:
            run.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def api_abort(request: Request) -> JSONResponse:
    run_id = request.path_params["run_id"]
    run = _RUNS.get(run_id)
    if run is None:
        return JSONResponse({"error": "unknown run_id"}, status_code=404)
    body = await request.json()
    leg = str(body.get("leg") or "")
    ev = run.aborts.get(leg)
    if ev is None:
        return JSONResponse({"error": f"unknown leg {leg!r}"}, status_code=404)
    ev.set()
    return JSONResponse({"aborted": leg})


routes = [
    Route("/", homepage),
    Route("/api/models", api_models),
    Route("/api/consensus", api_start, methods=["POST"]),
    Route("/api/consensus/{run_id}/events", api_events),
    Route("/api/consensus/{run_id}/abort", api_abort, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory=_WEB), name="static"),
]

app = Starlette(routes=routes)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8055)
