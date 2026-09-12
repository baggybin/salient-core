# brainstorm_conference (example)

A **conference / brainstorm engine** on the salient-core kernel: several LLM
model-agents argue a seed topic *dynamically* — hearing each other and building
on it in one shared thread — with a **hybrid moderator** (an LLM chair picks who
speaks next; a deterministic convergence score calls time), a **rapporteur** that
hands back a *map of the disagreement*, and an **idea leaderboard** ranked by
cross-model support.

The design goal is **divergence**, not consensus — the engine's job is to fight
the sycophantic collapse where several models politely average into mush. It's
meant as the engine *under* an ideation ritual (e.g. `uberbrainstorm`).

## Architecture — the honest version

The original plan aimed to run this on the kernel's full **daemon** (bus,
talk-only agents, operator inbox). Exploration showed that's a heavy lift the
kernel's own examples deliberately skip, and it *fights* a hybrid moderator: the
daemon runs agents autonomously, but we want deterministic control over who
speaks and when it stops. So this takes the lighter path the `consensus_panel`
example uses — a small orchestrator we drive — while **reusing the kernel's real
value as libraries**:

| Piece | Reused from the kernel |
|-------|------------------------|
| Debater bench | `polybrain.OpenAICompatBrain` + `BRAIN_SPECS` — the kernel's own OpenAI-compatible model client |
| Convergence score | `bus._consensus.semantic_agreement` — the exact measure `ask_consensus` uses |
| Idea clustering | `memory.embeddings.cosine` + a deterministic offline `HashEmbedder` |
| Model catalog | `polybrain.MODEL_REGISTRY` — per-brain model lists for the picker |

Only two things are genuinely new: the **forum** (an append-only shared thread —
the kernel talks star-shaped, no round table) and the **moderator** (the kernel
ships no speaker-selection or convergence policy, by design). **Zero kernel edits.**

## Roster

Debaters reach their models through the kernel's provider client (or a small
Anthropic client for Claude), so the bench is the wider ask-fable roster:

- **built-in brains:** `deepseek`, `glm`, `minimax` (from the kernel's `BRAIN_SPECS`)
- **added endpoints:** `openrouter`, `atlas` (OpenAI-compatible; Atlas base URL confirmed
  against `ask-fable`, env-overridable via `ASK_FABLE_ATLAS_BASE_URL`)
- **Claude seats:** `claude` / `sonnet` / `opus` / `fable` via a tiny Anthropic Messages
  client — `ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN` (+ `ANTHROPIC_BASE_URL`) for the
  OAuth/proxy path that reaches fable/opus.

## Picking models

Every provider family and its selectable models are enumerated in `catalog.py`
(offline — reuses the kernel's `MODEL_REGISTRY`). Two front-ends over it:

- **Web dialog** — `web_server.py` + `web/` (Starlette): a pop-up picker page that
  lists each provider (greying out those with no key), lets you choose models per
  seat, runs the conference, and renders the map + leaderboard + transcript.
- **Terminal picker** — `run.py --pick`, a zero-dependency selector over the same
  catalog.

## Files

| File | Role |
|------|------|
| `forum.py` / `forum_tools.py` | append-only thread + its bus-tool adapters |
| `panel.py` | the debater bench (`Seat`, `Panelist`, roster, Claude routing) |
| `anthropic_compat.py` | tiny Anthropic Messages client for Claude seats |
| `convergence.py` | `semantic_agreement` wrapper + offline `HashEmbedder` |
| `moderator.py` | the LLM chair (speaker selection, round-robin fallback) |
| `leaderboard.py` | cluster ideas by meaning, rank by cross-model support |
| `rapporteur.py` | map of the disagreement + the leaderboard |
| `conference.py` | the round loop tying it all together |
| `catalog.py` | selectable-models catalog (the picker's data layer) |
| `run.py` | live CLI entry point (`--pick` for model selection) |
| `web_server.py` / `web/` | Starlette web picker dialog + runner UI |

## Status

- [x] Forum primitive (`forum.py`, `forum_tools.py`) — tested
- [x] Debater bench on the kernel's provider client (`panel.py`) — tested
- [x] Convergence scoring via kernel `semantic_agreement` (`convergence.py`) — tested
- [x] Hybrid moderator: LLM speaker-pick + deterministic stop (`moderator.py`, `conference.py`) — tested
- [x] Rapporteur: map of the disagreement (`rapporteur.py`)
- [x] Idea leaderboard: cluster + rank by cross-model support (`leaderboard.py`) — tested
- [x] Claude seats via a small Anthropic client (`anthropic_compat.py`) — tested
- [x] Model catalog + terminal picker (`catalog.py`, `run.py --pick`) — tested
- [x] Web pop-up picker dialog + runner UI (`web_server.py`, `web/`) — tested
- [x] Live entry point (`run.py`)
- [ ] Durable, cross-session leaderboard: assert clusters into the kernel KG (noisy-OR)
- [ ] Swap the real `Embedder` in for live semantic scoring

## Run

Tests (offline — no keys, no network):

```sh
uv run pytest examples/brainstorm_conference/ -q
```

Live conference (needs at least two seat keys):

```sh
DEEPSEEK_API_KEY=… GLM_API_KEY=… MINIMAX_API_KEY=… \
  uv run python examples/brainstorm_conference/run.py "shard the DB by tenant or by customer?"

# or pick the bench interactively:
uv run python examples/brainstorm_conference/run.py --pick "…topic…"
```

Web picker dialog (from this directory):

```sh
uv run uvicorn web_server:app --reload   # then open http://127.0.0.1:8000
```
