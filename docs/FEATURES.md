# Core Features

This document provides a detailed breakdown of the features provided by `salient-core`.

| Feature | Description |
|---|---|
| **Default-deny policy gates** | Scope + safeguards on every tool call, under the model. Transport-neutral: SDK built-ins, bus tools, external MCP, text commands, and provider runtimes. Shadow mode first, then flip `enforce_builtin_policy: true`. |
| **Operator inbox** | Typed Q/A for human decisions. Delegation and policy walls become tickets — not silent failures or free rein. |
| **Redacted audit trail** | Replayable record of gate decisions and tool I/O. Secrets redacted; if a record can't be written, the store flags itself degraded rather than staying quiet. |
| **Token budgets** | Operator-set ceilings enforced below the model — warn, park, or interrupt. The spend ledger lives on the daemon and is epoch-keyed, so restarting an agent can't launder its spend. |
| **Provable stop** | `quiesce()` returns *evidence* the agent died — task state, model-subprocess pid, tool pids reaped vs survived, cgroup emptiness. When it can't prove it, it says `unverified`. |
| **Turn reconstruction** | One `correlation_id` joins job, prompt hash, questions, denies, remote calls, and cost into one chain — cross-checked against the authoritative store so a dropped record shows up as a gap, not a clean history. |
| **Typed MCP bus** | 31 inter-agent tools (delegation, context, KG, discovery, audit) as one MCP server per agent, plus `extra_tools` for domain add-ons. |
| **Noisy-OR knowledge graph** | Cross-session memory with corroboration, embeddings, subject namespaces, provenance, and archive-first compaction. |
| **Pluggable runtimes** | Claude Agent SDK by default; OpenAI Codex via `salient-core[codex]`; OpenAI-compatible API sub-brains via `polybrain`. Third-party providers register through an entry point — and inherit the gates by construction. |
| **Per-agent isolation** | One tool surface per agent; optional OS privilege separation via `_launch_profile`. |
