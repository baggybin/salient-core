# ask_fable

A gated MCP server that lets any agent — in Claude Code or opencode, including
agents running on DeepSeek/MiniMax/local models — request **narrow code/
architecture reasoning** from the Fable model (`claude-fable-5`).

Fable is an Anthropic model with no third-party route, and you may have no
Anthropic API key — so `ask_fable` reaches Fable through **Claude Code's existing
OAuth session** (via the Claude Agent SDK in-process; the `claude` CLI print mode
is a documented fallback). No API key is set.

## The guard (two layers + a model contract)

Every question is checked **before any model call**:

1. **Sanity floor** — rejects only empty, too-short (`<3` chars), too-long
   (`>4000` chars), or oversized context (`>20000` chars). This is NOT a breadth
   filter: broad engineering questions are allowed. Runs first (the denylist
   passes empty strings through).
2. **Reused prohibited-use denylist** — `salient_core.policy.safeguards.check_prompt_intent`.
   Anything about security testing / attacks / offensive tooling is hard-rejected.
3. **Model scope contract** — Fable answers engineering questions related to the
   agent's software work (breadth is fine) and replies `REFUSED: <reason>` only
   for cyber/attack content or non-software domain knowledge (e.g. biology).

A denied question **never reaches Fable**. Every decision is appended to an
owner-only audit log (question hashed, not stored raw, by default).

## Install & run

```bash
pip install -e '.[ask-fable]'          # pulls the optional `mcp` dep
python3 -m salient_core.ask_fable      # or: ask-fable   (stdio transport)
```

## Tools

- **`ask(question, context="", session="default", reset=false)`** — guarded
  reasoning. Reuse the same `session` key for follow-ups (Fable keeps context
  server-side); a new key or `reset=true` starts a fresh topic (dumping the prior
  transcript to a file first).
- **`reset_session(session="default", save=true)`** — dump the transcript to
  `${XDG_STATE_HOME}/salient/ask_fable/sessions/<key>-<ts>.md` (when `save`) and
  clear it.

Responses are a single JSON object:
`{"status":"ok","answer":...,"session":...}` ·
`{"status":"refused","stage":"guard|model","reason":...}` ·
`{"status":"error","kind":"timeout|sdk_error|binary_missing","detail":...}`.

## Registration

**Claude Code** — `~/.claude/.claude.json` (root-owned; edit as the owner):
```json
{ "mcpServers": { "ask_fable": { "command": "python3", "args": ["-m","salient_core.ask_fable"], "env": {} } } }
```

**opencode** — `~/.config/opencode/opencode.json`:
```json
{ "mcp": { "ask_fable": { "type": "local", "command": ["python3","-m","salient_core.ask_fable"], "enabled": true } } }
```

**salient** — `agents.yaml` `mcp_servers` (consumed by `_wire_external_mcp_servers`):
```yaml
mcp_servers:
  ask_fable:
    type: stdio
    command: python3
    args: ["-m","salient_core.ask_fable"]
    tools: ["ask", "reset_session"]
```

## Env knobs

| Var | Default | Meaning |
|---|---|---|
| `ASK_FABLE_MIN_LEN` / `ASK_FABLE_MAX_LEN` | 3 / 4000 | question length bounds |
| `ASK_FABLE_MAX_CONTEXT_LEN` | 20000 | context cap |
| `ASK_FABLE_TIMEOUT` | 120 | per-turn wall-clock seconds |
| `ASK_FABLE_USE_CLI` | off | force the `claude` CLI bridge instead of the SDK |
| `ASK_FABLE_AUDIT_PATH` | `$XDG_STATE_HOME/salient/ask_fable/decisions.jsonl` | audit log |
| `ASK_FABLE_AUDIT_RAW` | off | store raw question/context (default: sha256 only) |
