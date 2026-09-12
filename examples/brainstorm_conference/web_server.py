"""Web model-picker + runner for the brainstorm conference (Starlette).

  GET  /              → the picker page (a pop-up dialog to choose the bench)
  GET  /api/catalog   → provider families, availability, and selectable models
  POST /api/run       → run a conference for the chosen seats; returns the result

Runs synchronously — a conference is a handful of model calls — and the page
shows a "running…" state until the result lands. Offline-safe: ``/api/catalog``
and request parsing need no keys; ``/api/run`` needs at least two chosen seats
whose keys are configured.

Run it:  uv run uvicorn web_server:app --reload   (from this directory)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from catalog import catalog
from conference import run_conference
from forum import ForumStore
from moderator import Moderator
from panel import MissingSeatKeyError, Seat, build_panelist
from rapporteur import Rapporteur
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

_HERE = Path(__file__).resolve().parent
_WEB = _HERE / "web"

# Persona fragments that differentiate the voices (kept in step with run.py).
_PERSONAS: dict[str, str] = {
    "deepseek": "You argue from rigorous, systems-level first principles.",
    "glm": "You attack assumptions and hunt the unconventional angle.",
    "minimax": "You push the boldest, most ambitious version of any idea.",
    "openrouter": "You bring an outside-lab perspective and challenge the house view.",
    "atlas": "You stress-test feasibility and call out hand-waving.",
    "claude": "You weigh trade-offs carefully and demand concrete evidence.",
}


def _catalog_payload() -> list[dict]:
    """The catalog as plain JSON for the picker."""
    return [
        {
            "brain": fam.brain,
            "available": fam.available,
            "models": [{"id": m.id, "label": m.label} for m in fam.models],
        }
        for fam in catalog()
    ]


class RunRequestError(ValueError):
    """A bad /api/run body — surfaced to the page as a 400, not a 500."""


def _parse_run_request(body: dict) -> tuple[str, list[Seat], int, float]:
    """Validate + shape a run request. Raises RunRequestError on bad input."""
    topic = (body.get("topic") or "").strip()
    if not topic:
        raise RunRequestError("topic is required")
    seats: list[Seat] = []
    for entry in body.get("seats") or []:
        brain = str(entry.get("brain") or "").strip()
        if not brain:
            continue
        model = (str(entry.get("model") or "").strip()) or None
        seats.append(Seat(brain, brain, model=model, persona=_PERSONAS.get(brain, "")))
    if len(seats) < 2:
        raise RunRequestError("pick at least two models for a conference")
    rounds = int(body.get("rounds") or 3)
    threshold = float(body.get("threshold") or 0.85)
    return topic, seats, rounds, threshold


async def homepage(_: Request) -> FileResponse:
    return FileResponse(_WEB / "index.html")


async def api_catalog(_: Request) -> JSONResponse:
    return JSONResponse({"families": _catalog_payload()})


async def api_run(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        topic, seats, rounds, threshold = _parse_run_request(body)
    except RunRequestError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    panelists: dict = {}
    skipped: list[str] = []
    for seat in seats:
        try:
            panelists[seat.label] = build_panelist(seat)
        except MissingSeatKeyError as exc:
            skipped.append(str(exc))
    if len(panelists) < 2:
        return JSONResponse(
            {"error": "need at least two chosen seats with keys set", "skipped": skipped},
            status_code=400,
        )

    chair = next(iter(panelists.values()))
    forum = ForumStore()
    try:
        result = await run_conference(
            seed=topic,
            panelists=panelists,
            moderator=Moderator(chair, list(panelists)),
            forum=forum,
            threshold=threshold,
            max_rounds=rounds,
            rapporteur=Rapporteur(chair),
        )
    except Exception as exc:  # noqa: BLE001 — surface to the page, not a bare 500
        return JSONResponse({"error": f"run failed: {exc}"}, status_code=500)
    finally:
        for panelist in panelists.values():
            await panelist.aclose()

    report = result.report
    return JSONResponse(
        {
            "transcript": [
                {"seq": e.seq, "round": e.round, "agent": e.agent, "text": e.text}
                for e in forum.read()
            ],
            "rounds": result.rounds,
            "converged": result.converged,
            "score": result.final_score,
            "map_text": report.map_text if report else "",
            "ideas": [
                {"label": i.label, "support": i.support, "supporters": list(i.supporters)}
                for i in (report.ideas if report else ())
            ],
            "chair": chair.label,
            "skipped": skipped,
        }
    )


routes = [
    Route("/", homepage),
    Route("/api/catalog", api_catalog),
    Route("/api/run", api_run, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory=_WEB), name="static"),
]

app = Starlette(routes=routes)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8066)
