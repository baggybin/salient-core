"""In-memory Fable conversation sessions + dump-on-reset.

A session keeps the resumable SDK ``session_id`` (so follow-ups continue Fable's
context server-side) plus a lightweight text transcript used only for the
dump-to-file feature. State lives in the server process and clears on restart.

"New topic → dump/store then clear": ``reset`` writes the transcript to a
per-session markdown file (0600) under the state dir when ``save`` is set, then
drops the session so the next ``ask`` starts fresh.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sessions_dir() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state) / "salient" / "ask_fable" / "sessions"


@dataclass
class Session:
    key: str
    sdk_session_id: str | None = None
    turns: list[dict] = field(default_factory=list)  # {"q","a","ts"}
    started: float = field(default_factory=time.time)


class SessionStore:
    """Process-lifetime map of session key → Session."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, key: str) -> Session:
        s = self._sessions.get(key)
        if s is None:
            s = Session(key=key)
            self._sessions[key] = s
        return s

    def resume_id(self, key: str) -> str | None:
        s = self._sessions.get(key)
        return s.sdk_session_id if s else None

    def record_turn(self, key: str, question: str, answer: str, session_id: str | None) -> None:
        s = self.get(key)
        if session_id:
            s.sdk_session_id = session_id
        s.turns.append(
            {
                "q": question,
                "a": answer,
                "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )

    def reset(self, key: str, *, save: bool = True) -> str | None:
        """Drop the session. When ``save`` and it has turns, first write the
        transcript to a file and return its path (str); else return None."""
        s = self._sessions.pop(key, None)
        if s is None or not s.turns:
            return None
        if not save:
            return None
        return _dump(s)


def _dump(s: Session) -> str | None:
    try:
        d = _sessions_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        slug = _SLUG_RE.sub("_", s.key).strip("_") or "session"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = d / f"{slug}-{stamp}.md"
        lines = [f"# ask_fable session: {s.key}", ""]
        for i, t in enumerate(s.turns, 1):
            lines += [
                f"## Turn {i} — {t['ts']}",
                "",
                "**Q:**",
                "",
                t["q"],
                "",
                "**A:**",
                "",
                t["a"],
                "",
            ]
        path.write_text("\n".join(lines), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path)
    except Exception as exc:  # noqa: BLE001 — dump must never break the tool
        print(f"ask_fable: session dump failed: {exc}", file=sys.stderr)
        return None
