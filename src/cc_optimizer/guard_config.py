"""Settings and local state for the context guards.

Everything lives under ~/.claude/cc-optimizer (override with CC_OPTIMIZER_HOME):
  config.json     user overrides of DEFAULTS
  state/<id>.json per-session guard state
  events.jsonl    one line per guard action, for measuring the guards
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # Context length warning, shown once per band above the threshold.
    "context_warn_tokens": 300_000,
    "context_step_tokens": 100_000,
    # A session open this many hours is warned once, then again every step. 0 turns it off.
    "session_age_hours": 8,
    "session_age_step_hours": 4,
    # Rewriting an existing file with Write: "off", "coach" (default) or "block".
    "write_guard": "coach",
    "write_min_lines": 120,
    "write_unchanged_ratio": 0.6,
    # Rewriting an existing file through a shell heredoc (cat > file <<EOF).
    "heredoc_guard": True,
    # Reading a large file whole (no offset/limit): "off", "coach" or "block" (default).
    # Blocking pays off here: the content has not entered the context yet. A retry is allowed.
    "read_guard": "block",
    "read_guard_tokens": 6_000,
    # Model routing: delegation rules at session start (only when the subagents are installed),
    # and a one-time model suggestion on the first prompt, while switching is still cheap.
    "model_routing": True,
    # Remind the main conversation of the scout subagent after this many searches/reads in a row.
    "routing_nudge_after": 4,
    "model_advice": True,
    "model_advice_max_context": 80_000,
    # Project map file (.claude/project-map.md), refreshed at session start. Only its path enters the context.
    "project_map": True,
    # Permanent token history (~/.claude/cc-optimizer/history.db), synced in the background at session start.
    "history": True,
    # Work log (.claude/worklog.md): what earlier sessions did. A few one-line summaries enter the context.
    "worklog": True,
    "worklog_recent": 3,
    # Short token-efficiency rules added at session start and after compaction.
    "session_guidance": True,
    # Language of the command line output: "auto" (system language), "en" or "tr".
    "language": "auto",
}


def home() -> Path:
    override = os.environ.get("CC_OPTIMIZER_HOME")
    return Path(override) if override else Path.home() / ".claude" / "cc-optimizer"


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        data = json.loads((home() / "config.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def _state_path(session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_") or "unknown"
    return home() / "state" / f"{safe}.json"


def load_state(session_id: str) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(session_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(session_id: str, state: dict[str, Any]) -> None:
    path = _state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def heartbeat(event: str) -> None:
    """Record that a hook ran, as a file modification time, so `doctor` can tell silent failures."""
    if os.environ.get("CC_OPTIMIZER_NO_HEARTBEAT"):
        return  # doctor's own test runs must not look like real activity
    path = home() / "heartbeat" / event
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


def last_heartbeats() -> dict[str, float]:
    try:
        return {p.name: p.stat().st_mtime for p in (home() / "heartbeat").iterdir() if p.is_file()}
    except OSError:
        return {}


def log_event(kind: str, session_id: str, **fields: Any) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "kind": kind, "session": session_id, **fields}
    try:
        path = home() / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
