# Consensus panel — a bus showcase

Fan one question to a panel of models, stream each leg's reasoning trace into
its own pane, abort any leg mid-run, and see the **semantic convergence** score
and **judge verdict** the bus computes.

This demonstrates the `ask_consensus` machinery in `salient_core.bus._consensus`:
the same-prompt fan-out, per-leg trace capture, embedding-based
`semantic_agreement`, and the parameterizable judge.

## Run it

```sh
cd examples/consensus_panel
uvicorn server:app --reload      # http://127.0.0.1:8055
```

No API key needed: the default `MockPanelRunner` streams deterministic answers
and traces so the UI is fully demonstrable offline. Convergence is scored for
real by the kernel's `semantic_agreement` (via a deterministic hash embedder),
and the model picker uses the live Anthropic Models API when `anthropic` is
installed and a credential is present, falling back to a static catalog
otherwise.

## Wiring live models

`server.py` holds a single `RUNNER` and `runner.py` defines the event contract
(`Event`: `run_start` / `leg_start` / `trace` / `answer` / `aborted` /
`convergence` / `done`). A real bus-backed runner is a drop-in: emit those same
events from `AgentRunner` output and route the judge to `ask_consensus`'s
`judge_agent` on the chosen `judge_model`.

## Tests

```sh
python -m pytest        # runner event contract + Starlette endpoints (12 tests)
```
