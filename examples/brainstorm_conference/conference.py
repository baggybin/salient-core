"""The orchestrator — runs the conference round by round.

This is the loop the whole example exists to run:

1. Seed the forum thread with the topic.
2. **Round 1 is blind** — every panelist answers the topic alone (no thread), the
   answers are committed, then revealed together, so a later speaker can't just
   autocomplete an emerging consensus instead of forming its own position.
3. From round 2, each turn the **chair** (LLM) reads the thread, picks who speaks
   next + a directive, and that panelist posts its contribution to the forum.
4. After each round, score convergence over each speaker's latest post. Stop when
   it crosses ``threshold`` (converged) or when ``max_rounds`` is hit.
5. Hand the whole transcript to the rapporteur for the map of the disagreement.

Everything it touches is injected (panelists, chair, forum, embedder,
rapporteur), so the entire loop runs offline against stubs in the tests — no
model calls, no network.
"""

from __future__ import annotations

from dataclasses import dataclass

from convergence import convergence_score
from forum import ForumStore
from rapporteur import Rapporteur
from roles import Chair, Report, Speaker

DEBATER_SYSTEM = """You are a participant in a brainstorming conference with other \
AI models. Read the discussion thread, then add ONE contribution: a new idea, a \
concrete build on someone else's, or a sharp disagreement where you genuinely \
disagree. Do NOT just agree to be agreeable and do NOT summarise — the value is in \
pushing the thinking somewhere it hasn't been. Be brief and specific."""

BLIND_SYSTEM = """You are opening a brainstorming conference. No one has spoken yet \
— give YOUR own take on the topic, cold: the ideas, angles, and positions you think \
matter most, and stake out a clear stance. Be specific and concrete; do not hedge \
toward a safe middle."""


@dataclass(slots=True)
class ConferenceResult:
    """Outcome of a run: how many rounds ran, whether it converged, the final
    convergence score, and the rapporteur's report (if one was supplied)."""

    rounds: int
    converged: bool
    final_score: float | None
    report: Report | None


async def run_conference(
    *,
    seed: str,
    panelists: dict[str, Speaker],
    moderator: Chair,
    forum: ForumStore,
    embedder: object | None = None,
    threshold: float = 0.85,
    max_rounds: int = 3,  # 3-round default; callers can request more
    rapporteur: Rapporteur | None = None,
) -> ConferenceResult:
    """Run the conference to convergence or ``max_rounds``. See module docstring."""
    if not panelists:
        raise ValueError("a conference needs at least one panelist")

    forum.post("moderator", f"TOPIC: {seed}", round=0)

    # Round 1 — BLIND: every panelist answers the topic alone (no thread), the
    # answers are committed, then revealed together. Stops a later speaker from
    # autocompleting an emerging consensus instead of forming its own position.
    blind_prompt = f"TOPIC: {seed}"
    blind: dict[str, str] = {}
    for label, panelist in panelists.items():
        text = await panelist.speak(system=BLIND_SYSTEM, thread=blind_prompt)
        if text:
            blind[label] = text
    for label, text in blind.items():
        forum.post(label, text, round=1)

    round_no = 1
    score: float | None = await convergence_score(blind, embedder)
    converged = score is not None and score >= threshold
    last_speaker: str | None = None

    # Rounds 2..max — open, hybrid-moderated argument over the revealed thread.
    while not converged and round_no < max_rounds:
        round_no += 1
        latest: dict[str, str] = {}
        for _ in range(len(panelists)):
            thread = forum.transcript()
            turn = await moderator.next_turn(
                thread=thread, round=round_no, last_speaker=last_speaker
            )
            last_speaker = turn.speaker
            speaker = panelists.get(turn.speaker)
            if speaker is None:
                # Chair named someone not seated — skip defensively rather than crash.
                continue
            system = f"{DEBATER_SYSTEM}\n\nCHAIR'S DIRECTIVE: {turn.instruction}"
            text = await speaker.speak(system=system, thread=thread)
            if text:
                forum.post(turn.speaker, text, round=round_no)
                latest[turn.speaker] = text

        score = await convergence_score(latest, embedder)
        if score is not None and score >= threshold:
            converged = True
            break

    report = await rapporteur.summarize(forum) if rapporteur else None
    return ConferenceResult(
        rounds=round_no, converged=converged, final_score=score, report=report
    )
