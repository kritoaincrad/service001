"""Permanent per-session token history, independent of Claude Code's transcript cleanup.

Claude Code deletes transcripts after about 30 days, so every comparison that reads them
loses its "before" side over time. This module stores the numbers of each session (no
content) in ~/.claude/cc-optimizer/history.db and keeps them after the transcripts are gone.

Each session is labelled from its own transcript: when the guards were active, the text
they add at session start is in it. So "with Service001" and "without" are facts per
session, not a guess from the install date.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .guard_config import home

SCHEMA_VERSION = 1
DEFAULT_CALIBRATION = 1.7  # measured on the first baseline; used until enough samples exist
SYNC_INTERVAL = 3600  # background sync at most once an hour
# Text that only the guards put into a transcript.
GUARD_MARKERS = (b"Token efficiency rules for this session", b"[token guard]", b"[model advice]",
                 b"Model routing: delegate", b"cc-optimizer project map")
SOURCES = {"read": ("tool:Read",), "shell": ("tool:Bash", "tool:PowerShell"), "write": ("tool:Write",),
           "output": ("model_output",)}

COLUMNS = {
    "session_id": "TEXT PRIMARY KEY", "project": "TEXT", "title": "TEXT", "first_ts": "TEXT", "last_ts": "TEXT",
    "guarded": "INTEGER", "calls": "INTEGER", "main_calls": "INTEGER", "processed": "INTEGER",
    "subagent_processed": "INTEGER", "cache_read": "INTEGER", "output": "INTEGER", "context_p50": "REAL",
    "pre_compact_p50": "REAL", "startup": "REAL", "orientation": "REAL", "compactions": "INTEGER",
    "large_reads": "INTEGER", "carried_all": "REAL", "carried_read": "REAL", "carried_shell": "REAL",
    "carried_write": "REAL", "carried_output": "REAL", "models_main": "TEXT", "models_sub": "TEXT",
    "calibration": "REAL", "signature": "TEXT", "synced_at": "TEXT",
}


def db_path() -> Path:
    return home() / "history.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute(f"CREATE TABLE IF NOT EXISTS sessions ({', '.join(f'{k} {v}' for k, v in COLUMNS.items())})")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    return con


def _meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def _signature(files: list[Path]) -> str:
    parts = []
    for f in sorted(files):
        try:
            st = f.stat()
            parts.append(f"{f.name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            continue
    return "|".join(parts)


UNGUARDED, GUARDED, MIXED = 0, 1, 2


def guard_label(main_transcript: Path) -> int:
    """GUARDED when the guards were active from the first model call on, MIXED when they were
    installed during the session (excluded from comparisons), UNGUARDED otherwise."""
    try:
        data = main_transcript.read_bytes()
    except OSError:
        return UNGUARDED
    if not any(marker in data for marker in GUARD_MARKERS):
        return UNGUARDED
    for line in data.splitlines():
        if any(marker in line for marker in GUARD_MARKERS):
            return GUARDED
        if b'"type":"assistant"' in line.replace(b" ", b"") and b'"usage"' in line:
            return MIXED
    return GUARDED


def session_row(timelines: list, report: dict[str, Any], guarded: int, calibration: float, signature: str) -> dict:
    main = next(tl for tl in timelines if not tl.is_subagent)
    tokens, context = report["tokens"], report["context"]
    carried = {s["name"]: s["carried_tokens"] for s in report["sources"]}
    by_model = tokens.get("calls_by_model", {})
    return {
        "session_id": main.session_id,
        "project": main.project,
        "title": main.title,
        "first_ts": min(tl.first_ts or "" for tl in timelines) or None,
        "last_ts": max(tl.last_ts or "" for tl in timelines) or None,
        "guarded": guarded,
        "calls": tokens["calls"],
        "main_calls": len(main.calls),
        "processed": tokens["processed"],
        "subagent_processed": round(tokens["processed"] * tokens["subagent_share"]),
        "cache_read": tokens["cache_read"],
        "output": tokens["output"],
        "context_p50": context["per_call_p50"],
        "pre_compact_p50": context["pre_compact_p50"],
        "startup": context["startup_p50"],
        "orientation": context.get("orientation_p50"),
        "compactions": sum((context.get("compactions") or {}).values()),
        "large_reads": report["reads"]["large_full_reads"]["count"],
        "carried_all": sum(carried.values()),
        **{f"carried_{k}": sum(carried.get(n, 0) for n in names) for k, names in SOURCES.items()},
        "models_main": json.dumps(by_model.get("main", {})),
        "models_sub": json.dumps(by_model.get("subagent", {})),
        "calibration": calibration,
        "signature": signature,
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def sync(root: Path, con: sqlite3.Connection | None = None, fast: bool = False) -> dict[str, int]:
    """Record new and changed sessions. Rows of deleted transcripts are kept."""
    from .analyze import analyze, calibration_factor
    from .transcripts import TokenCounter, find_transcripts, parse_timeline

    own = con is None
    con = con or connect()
    known = {r["session_id"]: r["signature"] for r in con.execute("SELECT session_id, signature FROM sessions")}
    groups: dict[tuple[str, str], list[tuple[Path, bool]]] = defaultdict(list)
    for path, proj, sid, is_sub in find_transcripts(root):
        groups[(proj, sid)].append((path, is_sub))

    changed = {key: files for key, files in groups.items()
               if known.get(key[1]) != _signature([p for p, _ in files])}
    counter = TokenCounter(fast=fast)
    parsed = {
        key: [parse_timeline(p, key[0], key[1], is_sub, counter) for p, is_sub in files]
        for key, files in changed.items()
    }

    # One calibration for all sessions, so small sessions stay comparable with large ones.
    factor, samples = calibration_factor([tl for tls in parsed.values() for tl in tls])
    if samples >= 20:
        _set_meta(con, "calibration", str(factor))
    calibration = float(_meta(con, "calibration") or DEFAULT_CALIBRATION)

    written = 0
    for key, tls in parsed.items():
        tls = [tl for tl in tls if tl.calls]
        if not any(not tl.is_subagent for tl in tls):
            continue
        main_path = next(p for p, is_sub in changed[key] if not is_sub)
        report = analyze(tls, top=1, estimate_method=counter.method, calibration=calibration)
        row = session_row(tls, report, guard_label(main_path), calibration,
                          _signature([p for p, _ in changed[key]]))
        con.execute(f"INSERT OR REPLACE INTO sessions ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                    list(row.values()))
        written += 1
    _update_worklogs([next(p for p, is_sub in files if not is_sub) for files in changed.values()
                      if any(not is_sub for _, is_sub in files)], root)
    _set_meta(con, "last_sync", time.strftime("%Y-%m-%dT%H:%M:%S"))
    con.commit()
    if own:
        con.close()
    return {"transcripts": len(groups), "updated": written, "unchanged": len(groups) - len(changed)}


def _transcript_cwd(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(200), fh):
                if '"cwd"' in line:
                    try:
                        return json.loads(line).get("cwd")
                    except ValueError:
                        continue
    except OSError:
        pass
    return None


def _update_worklogs(main_transcripts: list[Path], root: Path) -> None:
    """Bring the work log of every project with changed sessions up to date."""
    from .guard_config import load_config
    from .project_map import is_project
    from .worklog import update

    if not load_config()["worklog"]:
        return
    for cwd in {c for c in map(_transcript_cwd, main_transcripts) if c}:
        if is_project(Path(cwd)):
            try:
                update(Path(cwd), root, budget_seconds=120)
            except OSError:
                continue


def maybe_sync_in_background(interval: float = SYNC_INTERVAL) -> bool:
    """Start a detached sync when the last one is older than `interval`. Never blocks the hook."""
    stamp = home() / "history.sync"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < interval:
            return False
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "cc_optimizer", "history", "--sync-only"], **kwargs)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ queries


def rows(con: sqlite3.Connection, project: str | None = None, guarded: bool | None = None,
         since: str | None = None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM sessions WHERE 1 = 1", []
    if project:
        sql += " AND lower(project) LIKE ?"
        args.append(f"%{project.lower().replace(':', '').replace('/', '-').replace(chr(92), '-')}%")
    if guarded is not None:
        sql += " AND guarded = ?"
        args.append(GUARDED if guarded else UNGUARDED)
    if since:
        sql += " AND first_ts >= ?"
        args.append(since)
    return con.execute(sql + " ORDER BY first_ts", args).fetchall()


def _weighted_median(pairs: Iterable[tuple[float, float]]) -> float | None:
    pairs = sorted((v, w) for v, w in pairs if v is not None and w)
    total = sum(w for _, w in pairs)
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= total / 2:
            return value
    return None


def _median(values: Iterable[float | None]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def as_report(selected: list[sqlite3.Row]) -> dict[str, Any]:
    """Aggregate session rows into the shape of an analyze report, for compare()."""
    calls = sum(r["calls"] for r in selected)
    processed = sum(r["processed"] for r in selected)
    carried_all = sum(r["carried_all"] for r in selected) or 0
    models: dict[str, Counter] = {"main": Counter(), "subagent": Counter()}
    for r in selected:
        models["main"].update(json.loads(r["models_main"] or "{}"))
        models["subagent"].update(json.loads(r["models_sub"] or "{}"))
    # One row per stored group, named after its first source (compare() sums Bash and PowerShell).
    sources = []
    for key, names in SOURCES.items():
        total = sum(r[f"carried_{key}"] for r in selected)
        sources.append({"name": names[0], "carried_tokens": total,
                        "carried_share": total / carried_all if carried_all else 0})
    return {
        "scope": {"sessions": len(selected), "first": min((r["first_ts"] or "" for r in selected), default=None),
                  "last": max((r["last_ts"] or "" for r in selected), default=None)},
        "tokens": {"calls": calls, "processed": processed,
                   "subagent_share": sum(r["subagent_processed"] for r in selected) / processed if processed else 0,
                   "calls_by_model": {k: dict(v) for k, v in models.items()}},
        "context": {"per_call_p50": _weighted_median((r["context_p50"], r["calls"]) for r in selected),
                    "pre_compact_p50": _median(r["pre_compact_p50"] for r in selected),
                    "startup_p50": _median(r["startup"] for r in selected),
                    "orientation_p50": _median(r["orientation"] for r in selected)},
        "reads": {"large_full_reads": {"count": sum(r["large_reads"] for r in selected)}},
        "sources": sources,
    }


def short_project(name: str) -> str:
    """Project folder name without the home prefix: C--Users-me-Desktop-App -> Desktop-App."""
    prefix = re.sub(r"[^A-Za-z0-9]", "-", str(Path.home())) + "-"
    return name[len(prefix):] if name.lower().startswith(prefix.lower()) else name


def _num(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


def _tokens(value: float) -> str:
    if value >= 1e9:
        return f"{value / 1e9:.1f}B".replace(".", ",")
    if value >= 1e6:
        return f"{value / 1e6:.0f}M"
    return f"{value / 1e3:.0f}K"


def render_history(con: sqlite3.Connection, project: str | None = None, weeks: int = 8) -> str:
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - weeks * 7 * 86400))
    selected = rows(con, project, since=since)
    every = rows(con, project)
    title = f"Token geçmişi{f' ({project})' if project else ''}"
    lines = [title, ""]
    if not every:
        return "\n".join(lines + ["Kayıt yok. Önce: cc-optimizer history --sync-only"])
    first = min(r["first_ts"] or "" for r in every)[:10]
    lines.append(f"Kayıtta {len(every)} oturum var, {first} tarihinden beri. Son {weeks} hafta:")
    lines.append("")

    weekly: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in selected:
        iso = date.fromisoformat((r["first_ts"] or "1970-01-01")[:10]).isocalendar()
        weekly[f"{iso[0]}-H{iso[1]:02d}"].append(r)
    header = f"{'Hafta':<10}{'Oturum':>8}{'Çağrı':>9}{'Token':>9}{'Token/çağrı':>13}{'Service001':>12}"
    lines += [header]
    for week in sorted(weekly):
        group = weekly[week]
        calls = sum(r["calls"] for r in group)
        processed = sum(r["processed"] for r in group)
        guarded = sum(r["guarded"] == GUARDED for r in group)
        lines.append(f"{week:<10}{len(group):>8}{_num(calls):>9}{_tokens(processed):>9}"
                     f"{_tokens(processed / calls if calls else 0):>13}{f'{guarded}/{len(group)}':>12}")

    if not project:
        lines += ["", f"Projeler (son {weeks} hafta):", f"{'Proje':<44}{'Oturum':>8}{'Token':>9}{'Token/çağrı':>13}"]
        by_project: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in selected:
            by_project[r["project"]].append(r)
        for name, group in sorted(by_project.items(), key=lambda kv: -sum(r["processed"] for r in kv[1]))[:10]:
            calls = sum(r["calls"] for r in group)
            processed = sum(r["processed"] for r in group)
            short = short_project(name)[:42]
            lines.append(f"{short:<44}{len(group):>8}{_tokens(processed):>9}{_tokens(processed / calls if calls else 0):>13}")
    mixed = sum(r["guarded"] == MIXED for r in selected)
    lines += ["", "Service001 sütunu: o haftaki oturumlardan kaçında koruyucuların baştan devrede olduğu."]
    if mixed:
        lines.append(f"{mixed} oturumda koruyucular oturumun ortasında kurulmuş. Bunlar karşılaştırmaya alınmaz.")
    return "\n".join(lines)
