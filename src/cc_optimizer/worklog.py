"""Per-project work log, so a new chat (or a compacted one) knows what was done before.

Every finished session of a project gets one entry in <project>/.claude/worklog.md: the
user's requests, the files that were changed, the commands that were run and the final
answer. Entries are extracted from the transcript by rules, so writing them costs no tokens.

At session start only a few one-line summaries and the file's path enter the context;
Claude reads the full entry when it continues earlier work.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .guard_config import home
from .shell_reads import READ_ONLY
from .transcripts import command_group, project_dir_name

LOG_NAME = "worklog.md"
MAX_ENTRIES = 40
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
_MARKER = re.compile(r"^<!-- session:(\S+) start:(\S+) -->$")
_SPACES = re.compile(r"\s+")
_BLOCKS = re.compile(r"<(pasted_content|system-reminder|ide_[a-z_]+)[^>]*>.*?</\1[^>]*>", re.S)
_PROGRAM = re.compile(r"^[A-Za-z][\w-]*$")


def projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def log_path(project: Path) -> Path:
    return project / ".claude" / LOG_NAME


def _clip(text: str, limit: int) -> str:
    text = _SPACES.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _local(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16]


def _prompt_texts(content: Any) -> list[str]:
    blocks = [content] if isinstance(content, str) else [
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    ] if isinstance(content, list) else []
    texts = []
    for block in blocks:
        # IDE notes, command output and system reminders start with a tag; they are not requests.
        if not block.strip() or block.lstrip().startswith(("<", "[Request interrupted")):
            continue
        texts.append(_BLOCKS.sub(lambda m: "[pasted]" if m.group(1) == "pasted_content" else "", block))
    return [t for t in texts if t.strip()]


def extract(transcript: Path) -> dict[str, Any] | None:
    """What happened in a session, from its main transcript. None when nothing was asked."""
    prompts: list[str] = []
    files: Counter = Counter()
    commands: Counter = Counter()
    entry: dict[str, Any] = {"session_id": transcript.stem, "title": None, "start": None, "end": None,
                             "cwd": None, "outcome": ""}
    try:
        handle = transcript.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            if '"user"' not in line and '"assistant"' not in line and "title" not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("isSidechain"):
                continue
            etype = e.get("type")
            if etype == "custom-title" and e.get("customTitle"):
                entry["title"] = e["customTitle"]
            elif etype == "ai-title" and e.get("aiTitle") and not entry["title"]:
                entry["title"] = e["aiTitle"]
            if etype not in ("user", "assistant"):
                continue
            entry["cwd"] = entry["cwd"] or e.get("cwd")
            if e.get("timestamp"):
                entry["start"] = entry["start"] or e["timestamp"]
                entry["end"] = e["timestamp"]
            content = (e.get("message") or {}).get("content")
            if etype == "user":
                if not e.get("isMeta") and not e.get("isCompactSummary"):
                    prompts += _prompt_texts(content)
                continue
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    entry["outcome"] = block["text"]
                elif block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    if block.get("name") in EDIT_TOOLS and isinstance(inp.get("file_path") or inp.get("notebook_path"), str):
                        files[inp.get("file_path") or inp.get("notebook_path")] += 1
                    elif block.get("name") in SHELL_TOOLS and isinstance(inp.get("command"), str):
                        group = command_group(inp["command"])
                        program = group.split()[0] if group else ""
                        if _PROGRAM.match(program) and program not in READ_ONLY:
                            commands[group] += 1
    if not prompts:
        return None
    entry.update(prompts=prompts, files=files, commands=commands)
    return entry


def _relative(path: str, cwd: str | None) -> str | None:
    """Path inside the project, or None for files elsewhere (scratch files, memory notes)."""
    try:
        return Path(path).resolve().relative_to(Path(cwd).resolve()).as_posix() if cwd else path
    except (ValueError, OSError):
        return None


def render_entry(entry: dict[str, Any]) -> str:
    prompts = entry["prompts"]
    shown = prompts if len(prompts) <= 5 else prompts[:2] + prompts[-3:]
    requests = " | ".join(f'"{_clip(p, 140)}"' for p in shown)
    if len(prompts) > 5:
        requests += f" (+{len(prompts) - 5} more)"
    title = entry["title"] or _clip(prompts[0], 70)
    lines = [f"<!-- session:{entry['session_id']} start:{entry['start'] or '-'} -->",
             f"### {_local(entry['start'])} - {_clip(title, 90)}",
             f"- Requests: {requests}"]
    inside = [(rel, n) for f, n in entry["files"].most_common() if (rel := _relative(f, entry["cwd"]))]
    if inside:
        changed = ", ".join(f"{rel} ({n})" for rel, n in inside[:12])
        more = len(inside) - 12
        lines.append(f"- Changed: {changed}" + (f" and {more} more" if more > 0 else ""))
    else:
        lines.append("- Changed: no files")
    if entry["commands"]:
        lines.append("- Commands: " + ", ".join(f"{_clip(c, 50)} ({n})" for c, n in entry["commands"].most_common(6)))
    if entry["outcome"]:
        lines.append(f"- Outcome: {_clip(entry['outcome'], 350)}")
    return "\n".join(lines)


def one_line(entry: dict[str, Any]) -> str:
    title = entry["title"] or _clip(entry["prompts"][0], 70)
    inside = [f for f, _ in entry["files"].most_common() if _relative(f, entry["cwd"])]
    files = [Path(f).name for f in inside[:3]]
    changed = f"changed {', '.join(files)}" + (f" +{len(inside) - 3}" if len(inside) > 3 else "") \
        if files else "no file changes"
    return f"{_local(entry['start'])} {_clip(title, 80)}: {changed}"


# ------------------------------------------------------------------ the log file


def _read_blocks(path: Path) -> dict[str, tuple[str, str]]:
    """session id -> (start timestamp, block text)."""
    blocks: dict[str, tuple[str, str]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return blocks
    current: list[str] = []
    key: tuple[str, str] | None = None
    for line in text.splitlines():
        match = _MARKER.match(line)
        if match:
            if key:
                blocks[key[0]] = (key[1], "\n".join(current).strip())
            key, current = (match.group(1), match.group(2)), [line]
        elif key:
            current.append(line)
    if key:
        blocks[key[0]] = (key[1], "\n".join(current).strip())
    return blocks


def _write(path: Path, blocks: dict[str, tuple[str, str]], project: Path) -> None:
    from .project_map import exclude_from_git

    newest = sorted(blocks.values(), key=lambda b: b[0], reverse=True)[:MAX_ENTRIES]
    header = (f"# Work log: {project.resolve().name}\n\n"
              "Written by cc-optimizer from session transcripts, newest first: what was asked, which files changed, "
              "which commands ran and how each session ended. Check the code before relying on details.\n\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n\n".join(b[1] for b in newest) + "\n", encoding="utf-8")
    exclude_from_git(project, f".claude/{LOG_NAME}")


def _signature(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def _state_file(project: Path) -> Path:
    return home() / "worklog" / f"{project_dir_name(project)}.json"


def update(project: Path, projects_root: Path, exclude_session: str | None = None,
           budget_seconds: float = 3.0) -> list[dict[str, Any]]:
    """Log new and changed sessions of `project`, newest first, within a time budget.

    Returns the entries of all logged sessions that are still available, newest first."""
    folder = projects_root / project_dir_name(project)
    state_path = _state_file(project)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    try:
        transcripts = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        transcripts = []
    started = time.monotonic()
    blocks = _read_blocks(log_path(project))
    entries: list[dict[str, Any]] = []
    changed = False
    for transcript in transcripts:
        sid = transcript.stem
        if sid == exclude_session:
            continue
        signature = _signature(transcript)
        if state.get(sid) == signature and sid in blocks:
            continue
        if time.monotonic() - started > budget_seconds:
            break  # the background sync logs the rest
        entry = extract(transcript)
        state[sid] = signature
        if entry:
            blocks[sid] = (entry["start"] or "", render_entry(entry))
            entries.append(entry)
            changed = True
    if changed:
        _write(log_path(project), blocks, project)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return entries


def recent(project: Path, exclude_session: str | None = None, count: int = 3) -> list[str]:
    """One-line summaries of the newest logged sessions, read back from the log file."""
    lines = []
    for sid, (_, block) in sorted(_read_blocks(log_path(project)).items(), key=lambda kv: kv[1][0], reverse=True):
        if sid == exclude_session:
            continue
        rows = block.splitlines()
        heading = next((r[4:] for r in rows if r.startswith("### ")), "")
        changed = next((r[len("- Changed: "):] for r in rows if r.startswith("- Changed: ")), "")
        names = [Path(part.rsplit(" (", 1)[0]).name for part in changed.split(", ")[:3]] if changed != "no files" else []
        lines.append(f"{heading}: " + (f"changed {', '.join(names)}" if names else "no file changes"))
        if len(lines) >= count:
            break
    return lines


def context_text(project: Path, session_id: str | None, transcript: str | None, source: str | None,
                 count: int = 3) -> str | None:
    """What enters the context at session start: the path, a few recent sessions and, after a
    compaction, a summary of this session so far."""
    path = log_path(project)
    lines = recent(project, session_id, count)
    current = extract(Path(transcript)) if source == "compact" and transcript else None
    if not lines and not current:
        return None
    text = [f"Work log of earlier sessions in this project: {path.as_posix()} (requests, changed files, outcomes; "
            "newest first). Read the matching entry before continuing or changing earlier work."]
    if lines:
        text.append("Recent sessions:\n" + "\n".join(f"- {line}" for line in lines))
    if current:
        text.append("This session before compaction: " + one_line(current))
    return "\n".join(text)
