"""Display & formatting helpers shared by main.py, daemon.py, salientctl.py, wizard.py.

ANSI color codes, the agent palette, kind→color mapping, and the async-safe
`_emit` printer all live here. Keeping these out of an entry-point module
avoids the layering inversion of importing main.py just to reuse a formatter.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

# Module-global so concurrent agents don't interleave their event lines.
_print_lock = asyncio.Lock()

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

# Hand-picked per-agent colors so each currently-shipped agent gets a
# visually distinct hue across xterm-256. Grouped by role so eyes can
# quickly tell related agents apart at a glance.
# Forks (`worker-2`, `analyst-3`) and subagents (`parent/sub`) share their
# source's color via _agent_color's key normalization below.
# Salient brand orange — matches the web UI's section labels, finding
# headers, and panel headings (hex #f0883e ≈ ANSI 208). Used here as
# the manager's accent (engagement coordinator gets the brand color),
# and re-exported for any future module that wants the same accent in
# CLI output.
SALIENT_ORANGE = "38;5;208"

# The kernel hand-picks a color only for the coordinator (the salient-brand
# orange). Every other agent — including a downstream skin's whole roster —
# resolves through the deterministic hash-picked fallback palette below, so no
# domain-specific agent names are baked into the kernel.
_AGENT_COLORS: dict[str, str] = {
    "manager": SALIENT_ORANGE,
}

# Fallback palette for spawned / unknown agents. Deterministic via name
# hash so a given agent name always lands the same color across runs.
#
# Deliberately excludes the orange / gold / yellow family (172, 179, 217,
# 221, 227) — that range is the brand-orange (SALIENT_ORANGE = 208)
# neighborhood and we want manager / section-label accents to stand out
# without spawned agents accidentally hashing to a visually similar tone.
# Replaced with cooler hues (cyan / sky / pink / purple).
_AGENT_FALLBACK_PALETTE = [
    "38;5;105",
    "38;5;49",
    "38;5;78",
    "38;5;203",
    "38;5;111",
    "38;5;135",
    "38;5;120",
    "38;5;200",
    "38;5;75",
    "38;5;81",
    "38;5;156",
    "38;5;212",
    "38;5;99",
    "38;5;219",
    "38;5;43",
    "38;5;177",
]


def _agent_color_key(name: str) -> str:
    """Derive the color-keying name. Strips parent/subagent paths and
    fork suffixes so derivative agents inherit their source's color
    (worker-2 takes worker's color; parent/sub takes parent's hue)."""
    base = name.split("/", 1)[0]
    # Strip a trailing -N fork suffix (worker-2, analyst-3) but NOT names that
    # legitimately end in digits like agent5 (no preceding dash).
    if "-" in base:
        head, _, tail = base.rpartition("-")
        if tail.isdigit() and head:
            base = head
    return base


_KIND_COLOR = {
    "start": "1;36",
    "text": "0",
    "thinking": "2;37",
    "tool-call": "36",
    "tool-result": "32",
    "tool-error": "31",
    "subagent-start": "1;33",
    "done": "1;35",
    "reply": "1;32",
    "system": "2;36",
    "question": "1;33",
    "suggestion": "1;32",  # bright green — advisory copilot nudge (idea 4.11)
    # Provenance events (2026-05-16) — keep in sync with KIND_COLOR
    # in salient/web/static/app.js.
    "user_message": "1;35",  # bright magenta — operator → agent
    "operator_answer": "1;35",  # same family — operator answer
    "peer_message": "38;5;117",  # soft cyan — agent → agent delegation
}


def _c(s: str, code: str) -> str:
    if not _USE_COLOR or code == "0":
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


def _agent_color(name: str) -> str:
    """Per-agent color, deterministic across runs. Hand-picked for known
    agents (each visually distinct); hash-bucketed across a wider fallback
    palette for spawned/forked names. Subagents and -N forks share their
    source's color."""
    key = _agent_color_key(name)
    if key in _AGENT_COLORS:
        return _AGENT_COLORS[key]
    # Hash → palette index. Use a stronger hash than sum-of-ords so similar
    # names ('worker' vs 'worker2') don't land in adjacent buckets.
    import hashlib

    h = int(hashlib.sha1(key.encode()).hexdigest()[:8], 16)
    return _AGENT_FALLBACK_PALETTE[h % len(_AGENT_FALLBACK_PALETTE)]


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + f"... [+{len(s) - n} chars]"


def _prettify_tool_name(name: str) -> str:
    """mcp__server__tool → server.tool; built-in names pass through.

    The server part is reverse-aliased through the alias layer so the
    daemon's downstream code (action ledger, CLI display, logs, web
    console) sees REAL agent names regardless of whether the SDK handed
    us an aliased server prefix — operator-facing surfaces keep the real
    names so logs are readable.

    Two shapes handled:
      mcp__<server>__<tool>            → <real-server>.<tool>
      mcp__bus__<owner>__<tool>...     → bus.<real-owner>.<tool>...
    """
    if not name.startswith("mcp__"):
        return name
    rest = name[len("mcp__") :]
    parts = rest.split("__")
    if not parts:
        return rest
    from .alias import to_real as _alias_to_real

    if len(parts) >= 3 and parts[0] == "bus":
        owner = _alias_to_real(parts[1])
        tool_parts = parts[2:]
        return "bus." + owner + "." + ".".join(tool_parts)
    server = _alias_to_real(parts[0])
    if len(parts) == 1:
        return server
    return server + "." + ".".join(parts[1:])


# Module-level silence flag. Set True to make every _emit call a
# no-op — used by the salient.sim scenario harness so REPL banners
# don't pollute scenario traces / JSON output. Several modules
# import _emit by direct binding (`from ..display import _emit`),
# so patching `display._emit` at runtime doesn't reach them; an
# in-function flag check does.
_silenced: bool = False


async def _emit(name: str, kind: str, text: str) -> None:
    """Print one event under a lock so concurrent agents don't interleave lines."""
    if _silenced:
        return
    ts = _c(time.strftime("%H:%M:%S"), "2;37")
    name_s = _c(f"[{name}]", _agent_color(name))
    kind_s = _c(f"{kind}:", _KIND_COLOR.get(kind, "0"))
    async with _print_lock:
        for line in text.splitlines() or [""]:
            print(f"{ts} {name_s} {kind_s} {line}")


def _stringify_tool_result(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _format_tool_args(args: dict[str, Any]) -> str:
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        if isinstance(v, str):
            vs = v.replace("\n", " ⏎ ")
            if len(vs) > 80:
                vs = vs[:80] + "…"
            if any(c in vs for c in ' "='):
                vs = '"' + vs.replace('"', '\\"') + '"'
        elif isinstance(v, (dict, list)):
            vs = json.dumps(v, default=str)
            if len(vs) > 80:
                vs = vs[:80] + "…"
        else:
            vs = str(v)
        parts.append(f"{k}={vs}")
    return " ".join(parts)


def _format_tool_result(text: str) -> str:
    text = text.strip()
    if text and text[0] in "{[":
        try:
            return json.dumps(json.loads(text), indent=2, default=str)
        except (ValueError, TypeError):
            pass
    return text


# ───────────────────────────────────────────────────────────────────────
#  Startup banners — shown when daemon boots and when client REPL opens.
#  Two flavors: a fuller "server" banner with live state, a compact one
#  for the REPL. Both honor NO_COLOR / non-tty.
# ───────────────────────────────────────────────────────────────────────

# ANSI-Shadow wordmark + arcade-cabinet "attract screen" frame. Picks up
# the project's metaphor (a salient — a forward-projecting position) and
# wraps it in arcade-game furniture: insert-coin-to-continue, two-player
# slots map to operator + daemon, the subsystems line names the four
# cooperating subsystems. Purely cosmetic; printed once at daemon boot
# and at REPL open.
_SALIENT_LOGO = (
    "         ▄████████    ▄████████  ▄█        ▄█     ▄████████ ███▄▄▄▄       ███\n"
    "         ███    ███   ███    ███ ███       ███    ███    ███ ███▀▀▀██▄ ▀█████████▄\n"
    "         ███    █▀    ███    ███ ███       ███▌   ███    █▀  ███   ███    ▀███▀▀██\n"
    "         ███          ███    ███ ███       ███▌  ▄███▄▄▄     ███   ███     ███   ▀\n"
    "       ▀███████████ ▀███████████ ███       ███▌ ▀▀███▀▀▀     ███   ███     ███\n"
    "                ███   ███    ███ ███       ███    ███    █▄  ███   ███     ███\n"
    "          ▄█    ███   ███    ███ ███▌    ▄ ███    ███    ███ ███   ███     ███\n"
    "        ▄████████▀    ███    █▀  █████▄▄██ █▀     ██████████  ▀█   █▀     ▄████▀\n"
    "                                ▀\n"
    "\n"
    "                   ┌─────────────────────────────────────┐\n"
    "                   │  INSERT COIN TO CONTINUE            │\n"
    "                   │                                     │\n"
    "                   │  1P: OPERATOR                       │\n"
    "                   │  2P: DAEMON                         │\n"
    "                   │                                     │\n"
    "                   │  MODE: MULTI-AGENT COORDINATION     │\n"
    "                   └─────────────────────────────────────┘\n"
    "\n"
    "                 [ AGENTS ]──[ BUS ]──[ MEMORY ]──[ LEDGER ]\n"
    "\n"
    "                         PRESS ENTER TO ORCHESTRATE"
)


def _banner_color(text: str, code: str) -> str:
    """Color the banner only when stdout is a TTY and NO_COLOR isn't set
    (matches the rest of display.py's gating)."""
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _banner_compose(*, fields: list[tuple[str, str]] | None = None) -> str:
    """Render the ANSI-Shadow wordmark + arcade attract-screen block,
    then append optional state lines below ('socket: …', 'agents: …').

    The wordmark gets bright cyan; the arcade-furniture lines (frame,
    teams, "PRESS ENTER") get dimmer cyan so the title still dominates.
    The first nine lines of `_SALIENT_LOGO` are the block-character
    title; everything below is the cabinet frame and tagline."""
    # Two leading blank lines so the banner doesn't sit flush against
    # the previous shell prompt / log line — gives it breathing room.
    out: list[str] = ["", ""]
    for i, ln in enumerate(_SALIENT_LOGO.split("\n")):
        code = "1;36" if i < 9 else "0;36"
        out.append(_banner_color(ln, code))
    if fields:
        # Right-pad the longest label so the values line up.
        label_w = max(len(k) for k, _ in fields) + 1
        out.append("")
        for k, v in fields:
            label = (k + ":").ljust(label_w)
            out.append("  " + _banner_color(label, "2;37") + " " + v)
    return "\n".join(out)


def banner_server(
    *,
    socket_path: str,
    agent_count: int,
    running_count: int,
    engagement: str | None = None,
    engagement_name: str | None = None,
    effort: str | None = None,
) -> str:
    """Boot banner for the daemon. Includes live state (socket, agent
    counts, engagement id) so the operator sees on the same screen
    what the daemon thinks it just started."""
    fields: list[tuple[str, str]] = [
        ("socket", _banner_color(socket_path, "1;36")),
        (
            "agents",
            _banner_color(
                f"{agent_count} configured · {running_count} starting",
                "1;36",
            ),
        ),
    ]
    if engagement_name:
        fields.append(("eng", _banner_color(engagement_name, "1;33")))
    if engagement:
        fields.append(("run", _banner_color(engagement, "1;33")))
    if effort:
        fields.append(("effort", _banner_color(effort, "1;33")))
    fields.append(
        (
            "tagline",
            _banner_color(
                "multi-agent coordination kernel — daemon ready",
                "2;36",
            ),
        )
    )
    return _banner_compose(fields=fields)


def banner_client(*, socket_path: str) -> str:
    """REPL-open banner. Smaller — just the wordmark + connection info,
    so it doesn't dominate the first screen of every session."""
    fields = [
        ("socket", _banner_color(socket_path, "1;36")),
        ("tip", _banner_color("type `help` for commands · `q` for questions", "2;36")),
    ]
    return _banner_compose(fields=fields)
