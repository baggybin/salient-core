"""Operator-curated per-agent lessons store (cross-engagement).

A plain-text markdown file per agent at `~/.salient/lessons/<agent>.md`.
The operator writes lessons via `salientctl lessons add <agent>
<text>`; the file's body gets injected into the agent's system prompt
at runner construction (see _augment_system_prompt). Agents read,
operator writes — defends against LLM-self-poisoning by construction.

Why files, not a DB:
  - Single human-curated source; SQL adds nothing.
  - Operator can `cat` / `vim` the file directly when they want to
    cull or restructure — no separate editor surface.
  - Atomic writes via tempfile + rename mean a crash mid-write leaves
    the previous version intact.

File shape (per agent):

    # 2026-05-28
    - Lower the crawler's concurrency to 4 on large sites — saw
      timeouts at 8 on project-acme.
    - Warm up with a small batch before going wide; saves the
      operator a re-prompt.

    # 2026-06-02
    - Operator confirmed the --verbose flag is required by their
      review pipeline; bake it in.

Day-stamp headers are added by `append()` on the first lesson per
day; subsequent same-day adds slot under the existing header. No
machine-readable structure beyond that — operators read these
end-to-end before each engagement; over-structuring kills that.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_DIR = Path.home() / ".salient" / "lessons"


def lessons_dir() -> Path:
    """Resolve the lessons directory. Honors SALIENT_LESSONS_DIR for
    tests / per-deployment overrides; defaults to ~/.salient/lessons/.
    Does NOT create the directory — write() / append() create on demand
    so a daemon that never accumulates lessons leaves no on-disk
    footprint."""
    override = os.environ.get("SALIENT_LESSONS_DIR")
    if override:
        return Path(override)
    return _DEFAULT_DIR


def _path(agent: str) -> Path:
    """Resolve the lessons file path for an agent name. Agent name is
    treated as a flat identifier — '/' or '..' would escape the dir
    and we refuse those rather than sanitizing silently."""
    if not agent or not agent.strip():
        raise ValueError("agent name required")
    if "/" in agent or "\\" in agent or ".." in agent or agent.startswith("."):
        raise ValueError(f"invalid agent name {agent!r}: must be a flat identifier")
    return lessons_dir() / f"{agent}.md"


def read(agent: str) -> str:
    """Return the agent's lessons file body, or "" when absent.
    Trailing whitespace is preserved so the appender's day-header
    spacing survives round-trips."""
    p = _path(agent)
    if not p.exists():
        return ""
    try:
        return p.read_text()
    except OSError:
        return ""


def write(agent: str, body: str) -> None:
    """Overwrite the agent's lessons file with `body`. Atomic via
    tempfile-in-same-dir + rename so a crash mid-write leaves the
    previous version intact (and an unrelated reader never sees a
    half-written file)."""
    p = _path(agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{agent}.",
        suffix=".tmp",
        dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append(agent: str, text: str, *, today: str | None = None) -> None:
    """Add a `- <text>` lesson under today's date header. If the file
    already ends with today's `# YYYY-MM-DD` header, slot the new line
    under it; otherwise add a fresh header (with a leading blank-line
    separator when the file already has content).

    `today` lets tests pin the date string; defaults to local time
    YYYY-MM-DD.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("lesson text required")
    day = today or time.strftime("%Y-%m-%d", time.localtime())
    existing = read(agent)
    header = f"# {day}"

    # Day-header re-use: trim trailing whitespace + check whether the
    # last header in the file matches today's date. If so, append
    # directly under it (no new header). Otherwise emit a fresh
    # header block.
    lines = existing.rstrip().splitlines()
    last_header_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("# "):
            last_header_idx = i
            break
    if last_header_idx is not None and lines[last_header_idx] == header:
        # Same-day add: append under existing header. Preserve any
        # trailing-blank-line convention the existing file has.
        new_body = existing.rstrip() + f"\n- {text}\n"
    else:
        prefix = (existing.rstrip() + "\n\n") if existing.strip() else ""
        new_body = f"{prefix}{header}\n- {text}\n"
    write(agent, new_body)


def clear(agent: str) -> bool:
    """Delete the agent's lessons file. Returns True if a file was
    removed, False if it never existed."""
    p = _path(agent)
    if not p.exists():
        return False
    p.unlink()
    return True


@dataclass(frozen=True)
class LessonsSummary:
    """One agent's lessons-file summary, surfaced by `lessons list` and
    `_cmd_info`'s `lessons:` field."""

    agent: str
    line_count: int  # data lines (`-` bullets), excludes day headers
    size_bytes: int  # raw file size on disk


def summary() -> list[LessonsSummary]:
    """All agents with non-empty lessons files. Sorted by agent name.
    Empty / missing files are excluded — the absence of a row is the
    signal."""
    out: list[LessonsSummary] = []
    d = lessons_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.md")):
        try:
            body = path.read_text()
        except OSError:
            continue
        size = len(body.encode("utf-8"))
        bullets = sum(1 for ln in body.splitlines() if re.match(r"^\s*-\s", ln))
        if size == 0 and bullets == 0:
            continue
        out.append(
            LessonsSummary(
                agent=path.stem,
                line_count=bullets,
                size_bytes=size,
            )
        )
    return out


# Soft warning threshold. Files larger than this are still injected;
# `_cmd_info` and `salientctl lessons list` flag them so the operator
# notices it's time to cull. The threshold is operator-tuned, not a
# hard cap — every per-agent surface is different.
SIZE_WARN_BYTES = 2048


def is_oversized(s: LessonsSummary) -> bool:
    return s.size_bytes > SIZE_WARN_BYTES
