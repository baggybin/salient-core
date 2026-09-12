"""Run a live brainstorm conference from the command line.

    uv run python examples/brainstorm_conference/run.py "should we cache X in Redis or Postgres?"

Wires real models onto the loop: a debater bench from the roster (seats whose API
keys are set), an LLM chair, and a rapporteur. Seats with no key are skipped and
reported; you need at least two debaters to hold a conference.

Keys (any subset): DEEPSEEK_API_KEY, GLM_API_KEY / ZHIPU_API_KEY, MINIMAX_API_KEY,
OPENROUTER_API_KEY, ATLASCLOUD_API_KEY. openrouter/atlas seats are added only when
their key is present. The chair defaults to CHAIR_BRAIN (or the first live seat).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from catalog import catalog
from conference import run_conference
from forum import ForumStore
from moderator import Moderator
from panel import DEFAULT_ROSTER, MissingSeatKeyError, Panelist, Seat, build_panelist
from rapporteur import Rapporteur

_PERSONAS: dict[str, str] = {
    "deepseek": "You argue from rigorous, systems-level first principles.",
    "glm": "You attack assumptions and hunt the unconventional angle.",
    "minimax": "You push the boldest, most ambitious version of any idea.",
    "openrouter": "You bring an outside-lab perspective and challenge the house view.",
    "atlas": "You stress-test feasibility and call out hand-waving.",
    "claude": "You weigh trade-offs carefully and demand concrete evidence.",
}

# Optional wider bench — added only if their keys/endpoints are configured.
_EXTENDED_SEATS: tuple[Seat, ...] = (
    Seat("openrouter", "openrouter", persona=_PERSONAS["openrouter"]),
    Seat("atlas", "atlas", persona=_PERSONAS["atlas"]),
    Seat("claude", "claude", persona=_PERSONAS["claude"]),
)


def _pick_seats() -> list[Seat]:
    """Interactive terminal picker: choose which providers/models sit the table.

    Renders the catalog (only families whose key is set are selectable); for each,
    Enter includes it on its default model, a number picks a specific model, 'n'
    skips it. A GUI dialog could drive the same ``catalog()`` — this is the
    zero-dependency version.
    """
    chosen: list[Seat] = []
    print("Pick your debaters — Enter=include (default model), a number=that model, n=skip:\n")
    for family in catalog():
        if not family.available:
            print(f"  {family.brain}: (no key set — unavailable)")
            continue
        print(f"\n{family.brain}:")
        for i, model in enumerate(family.models):
            print(f"    {i}) {model.id}  — {model.label}")
        answer = input(f"  include {family.brain}? [Y/n or model #]: ").strip().lower()
        if answer in ("n", "no"):
            continue
        model_id = None
        if answer.isdigit() and int(answer) < len(family.models):
            model_id = family.models[int(answer)].id
        chosen.append(
            Seat(family.brain, family.brain, model=model_id, persona=_PERSONAS.get(family.brain, ""))
        )
    return chosen


def _live_panelists(seats: list[Seat]) -> tuple[dict[str, Panelist], list[str]]:
    """Build every seat whose key is set; return (panelists, skipped-messages)."""
    panelists: dict[str, Panelist] = {}
    skipped: list[str] = []
    for seat in seats:
        try:
            panelists[seat.label] = build_panelist(seat)
        except MissingSeatKeyError as exc:
            skipped.append(str(exc))
    return panelists, skipped


def _chair(panelists: dict[str, Panelist]) -> Panelist:
    """The moderating/rapporteur LLM: CHAIR_BRAIN if it's live, else the first seat."""
    preferred = os.environ.get("CHAIR_BRAIN")
    if preferred and preferred in panelists:
        return panelists[preferred]
    return next(iter(panelists.values()))


async def _main(topic: str, *, rounds: int, threshold: float, pick: bool) -> int:
    seats = _pick_seats() if pick else [*DEFAULT_ROSTER, *_EXTENDED_SEATS]
    panelists, skipped = _live_panelists(seats)
    for msg in skipped:
        print(f"  (skipped) {msg}", file=sys.stderr)
    if len(panelists) < 2:
        print(
            "\nNeed at least two debaters with API keys set. Configure keys for the "
            "roster (e.g. DEEPSEEK_API_KEY, GLM_API_KEY, MINIMAX_API_KEY) and retry.",
            file=sys.stderr,
        )
        return 1

    chair = _chair(panelists)
    roster = list(panelists)
    print(f"Debaters: {', '.join(roster)}   Chair: {chair.label}\n")

    forum = ForumStore()
    result = await run_conference(
        seed=topic,
        panelists=panelists,
        moderator=Moderator(chair, roster),
        forum=forum,
        threshold=threshold,
        max_rounds=rounds,
        rapporteur=Rapporteur(chair),
    )

    print("=" * 72)
    print(forum.transcript())
    print("=" * 72)
    print(
        f"\nRounds: {result.rounds}   Converged: {result.converged}   "
        f"Score: {result.final_score}\n"
    )
    if result.report:
        print("── MAP OF THE DISAGREEMENT ──\n")
        print(result.report.map_text)
        if result.report.ideas:
            print("\n── IDEA LEADERBOARD (by cross-model support) ──\n")
            for idea in result.report.ideas:
                backers = ", ".join(idea.supporters)
                print(f"  [{idea.support}] {idea.label}  ({backers})")

    for panelist in panelists.values():
        await panelist.aclose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live brainstorm conference.")
    parser.add_argument("topic", help="the seed question to argue")
    parser.add_argument("--rounds", type=int, default=3, help="max rounds (default 3; raise for deep dilemmas)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="convergence score at which to stop (default 0.85)",
    )
    parser.add_argument(
        "--pick",
        action="store_true",
        help="interactively pick which providers/models sit the table",
    )
    args = parser.parse_args()
    return asyncio.run(
        _main(args.topic, rounds=args.rounds, threshold=args.threshold, pick=args.pick)
    )


if __name__ == "__main__":
    raise SystemExit(main())
