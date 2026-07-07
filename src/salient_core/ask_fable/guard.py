"""Two-layer request gate for ask_fable.

Every question is checked here BEFORE any model call. Layer 1 is a cheap
non-empty/size SANITY floor (NOT a breadth filter — broad engineering questions
are allowed); layer 2 reuses the kernel's existing prohibited-use denylist. The
model-side scope contract (prompts.FABLE_SYSTEM_PROMPT) is the third layer: it,
not this heuristic, decides scope — allowing broad engineering questions and
refusing only cyber/attack content and non-software domain knowledge (biology,
etc.).

Order matters: the safeguard returns (True, "") for empty/whitespace input, so
the sanity floor MUST run first to catch empty questions.
"""

from __future__ import annotations

import os

from salient_core.policy.safeguards import check_prompt_intent


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def check(question: str, context: str = "") -> tuple[bool, str]:
    """Return (allowed, reason). ``reason`` is "" when allowed, otherwise a short
    label safe to surface/log. No model is consulted here."""
    q = (question or "").strip()
    min_len = _int_env("ASK_FABLE_MIN_LEN", 3)
    max_len = _int_env("ASK_FABLE_MAX_LEN", 4000)
    max_ctx = _int_env("ASK_FABLE_MAX_CONTEXT_LEN", 20000)

    # Layer 1 — non-empty + size sanity ONLY (runs first; the safeguard passes
    # empty input through). Breadth is NOT filtered here: a broad engineering
    # question is legitimate; scope is decided by the model contract (layer 3).
    if not q:
        return False, "empty question"
    if len(q) < min_len:
        return False, f"question too short (min {min_len} chars)"
    if len(question) > max_len:
        return False, f"question too long (max {max_len} chars)"
    if len(context or "") > max_ctx:
        return False, f"context too large (max {max_ctx} chars)"

    # Layer 2 — reused prohibited-use denylist (black box; label is safe to log).
    allowed, label = check_prompt_intent(question)
    if not allowed:
        return False, label or "prohibited content"

    return True, ""
