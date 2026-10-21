"""Claude Code hook handlers for the context guards.

Each handler takes the hook input (parsed stdin JSON) and returns the JSON to
print, or None for "no opinion". Handlers never raise and never grant
permissions: without an explicit "block" setting they only add context.

Text meant for Claude is English (fewer tokens). Text meant for the user is Turkish.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .guard_config import heartbeat, load_config, load_state, log_event, save_state
from .i18n import L
from .shell_reads import file_prints, is_exploration

SESSION_GUIDANCE = (
    "Token efficiency rules for this session:\n"
    "- Change existing files in place with targeted edits. Do not rewrite a whole existing file "
    "to change part of it, neither with Write nor with a shell heredoc.\n"
    "- For large files, find the relevant part first (Grep), then read only that range (offset/limit).\n"
    "- Keep command output short: filter or tail long logs instead of printing them in full."
)

ROUTING_RULES = (
    "Model routing: match the model to the work and delegate self-contained subtasks. "
    "scout (Haiku) finds and reads code, logs, docs; worker (Sonnet) does specified implementation, tests, "
    "mechanical refactors; architect (Opus) takes design decisions, hard debugging, risky multi-file changes when "
    "you run on a smaller model; expert (Fable) only the hardest problems. "
    "If most of this session is advanced work on Haiku or Sonnet, tell the user once that /model opus fits better. "
    "Keep work that needs this conversation's context here. Ask subagents for short reports."
)

MODEL_ADVICE = (
    "[model advice] First request of the session: the context is still small, so switching models is cheap now. "
    "Later a switch re-processes the whole conversation without cache. Judge this task. If it is clearly simple "
    "(a question, a small edit, renaming, formatting, a short script), end your reply with one short line in the "
    "user's language suggesting /model haiku for the rest of this session. If it is routine work with a clear "
    "spec, suggest /model sonnet. If it is advanced (architecture, hard debugging, a large risky change), suggest "
    "/model opus. If it is exceptionally hard, suggest /model fable. Never suggest the model you are running on. "
    "If unsure, say nothing about models."
)

_TAIL_CHUNK = 256 * 1024
_TAIL_MAX = 8 * 1024 * 1024
_MAX_FILE_BYTES = 5 * 1024 * 1024


# ------------------------------------------------------------------ helpers


def _k(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{round(n / 1000)}K"


def last_context_tokens(transcript_path: str | None) -> int | None:
    """Context size of the last main-thread API call, read from the end of the transcript."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            window = _TAIL_CHUNK
            while True:
                start = max(size - window, 0)
                fh.seek(start)
                lines = fh.read(size - start).split(b"\n")
                if start > 0:
                    lines = lines[1:]  # first line may be cut
                for raw in reversed(lines):
                    if b'"assistant"' not in raw or b'"usage"' not in raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    if entry.get("type") != "assistant" or entry.get("isSidechain"):
                        continue
                    msg = entry.get("message") or {}
                    usage = msg.get("usage")
                    if msg.get("model") == "<synthetic>" or not isinstance(usage, dict):
                        continue
                    return sum(
                        int(usage.get(k) or 0)
                        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                    )
                if start == 0 or window >= _TAIL_MAX:
                    return None
                window *= 4
    except OSError:
        return None


def _resolve(path: str, cwd: str | None) -> Path:
    p = Path(path)
    return p if p.is_absolute() or not cwd else Path(cwd) / p


def _significant_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def rewrite_stats(existing: Path, new_content: str, min_lines: int) -> dict[str, int] | None:
    """How much of `new_content` repeats the existing file. None when not a rewrite worth flagging."""
    try:
        if not existing.is_file() or existing.stat().st_size > _MAX_FILE_BYTES:
            return None
        old_lines = _significant_lines(existing.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    new_lines = _significant_lines(new_content)
    # The cost is the size of what is written, so both versions must be large.
    if len(old_lines) < min_lines or len(new_lines) < min_lines:
        return None
    old_set = set(old_lines)
    unchanged = sum(1 for line in new_lines if line in old_set)
    return {"old_lines": len(old_lines), "new_lines": len(new_lines), "unchanged": unchanged}


# Heredoc that writes a file: `cat > f <<EOF`, `cat <<'EOF' > f`, `tee f <<EOF`. Appends (>>) are ignored.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_REDIRECT = re.compile(r"(?<![>0-9&])>(?!>)\s*(['\"]?)([^\s'\";&|<>]+)\1")
_TEE = re.compile(r"\btee\s+(?:-[A-Za-z]+\s+)*(['\"]?)([^\s'\";&|<>]+)\1")


def heredoc_writes(command: str) -> list[tuple[str, str]]:
    """Return (target path, body) for every heredoc in `command` that overwrites a file."""
    found: list[tuple[str, str]] = []
    lines = command.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _HEREDOC.search(line)
        if not match:
            i += 1
            continue
        delimiter = match.group(2)
        end = i + 1
        while end < len(lines) and lines[end].strip() != delimiter:
            end += 1
        target = _REDIRECT.search(line) or _TEE.search(line)
        if target and "$" not in target.group(2):
            found.append((target.group(2), "\n".join(lines[i + 1 : end])))
        i = end + 1
    return found


_NON_TEXT_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".ipynb"}
_MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdx"}
_CODE_OUTLINE = re.compile(
    r"^\s*(?:(?:export|default|public|private|protected|internal|static|abstract|async|pub)\s+)*"
    r"(?:def|class|function|interface|type|enum|struct|impl|fn|func|module|namespace|record|trait)\s+[A-Za-z_$]"
)
_HEADING_OUTLINE = re.compile(r"^#{1,3}\s+\S")
_OUTLINE_MAX = 40


def estimate_read_tokens(size_bytes: int) -> int:
    """Rough context cost of reading a file whole.

    o200k averages about 4 bytes per token on code. Read adds line numbers, and real context
    grows about 1.7x faster than that estimate (the analyzer's calibration), so about 2.5 bytes.
    """
    return round(size_bytes / 2.5)


def outline(text: str, suffix: str, limit: int = _OUTLINE_MAX) -> list[str]:
    """Declarations (code) or headings (Markdown) with line numbers, so a range can be read directly."""
    pattern = _HEADING_OUTLINE if suffix in _MARKDOWN_SUFFIXES else _CODE_OUTLINE
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if pattern.match(line):
            found.append(f"{number}: {line.strip()[:100]}")
            if len(found) >= limit:
                break
    return found


def _large_read(inp: dict[str, Any], session: str, cwd: str | None, cfg: dict[str, Any]) -> dict[str, Any] | None:
    path = inp.get("file_path")
    if not isinstance(path, str) or inp.get("offset") is not None or inp.get("limit") is not None:
        return None
    target = _resolve(path, cwd)
    suffix = target.suffix.lower()
    if suffix in _NON_TEXT_SUFFIXES:
        return None
    try:
        size = target.stat().st_size
        tokens = estimate_read_tokens(size)
        if not target.is_file() or tokens < int(cfg["read_guard_tokens"]):
            return None
        text = target.read_text(encoding="utf-8", errors="replace") if size <= _MAX_FILE_BYTES else ""
    except OSError:
        return None
    lines = len(text.splitlines()) if text else None
    details = {"path": path, "est_tokens": tokens, "lines": lines}
    message = (
        f"[token guard] {path} is large (about {_k(tokens)} tokens" + (f", {lines} lines" if lines else "") + "). "
        "Read whole, it stays in the context and is re-read on every following call. "
        "Find the part you need first (Grep with line numbers), then Read only that range with offset and limit."
    )
    return _guard_once(
        session, cfg, "read_large", _file_subject(target), details, message,
        outline(text, suffix), "If you really need the whole file, repeat the same Read once.",
    )


def _shell_read(command: str, tool: str, session: str, cwd: str | None, cfg: dict[str, Any]) -> dict[str, Any] | None:
    files = [p for p in file_prints(command, cwd, powershell=tool == "PowerShell")
             if p.suffix.lower() not in _NON_TEXT_SUFFIXES]
    if not files:
        return None
    try:
        sizes = {p: p.stat().st_size for p in files}
    except OSError:
        return None
    tokens = estimate_read_tokens(sum(sizes.values()))
    if tokens < int(cfg["read_guard_tokens"]):
        return None
    largest = max(files, key=lambda p: sizes[p])
    names = ", ".join(p.name for p in files[:5]) + (f" and {len(files) - 5} more" if len(files) > 5 else "")
    details = {"files": len(files), "est_tokens": tokens, "path": str(largest)}
    message = (
        f"[token guard] This command prints about {_k(tokens)} tokens of file content ({names}). "
        "Printed output stays in the context and is re-read on every following call. "
        "Find the part you need first (grep -n or Grep), then print only that range (sed -n 'A,Bp') "
        "or use Read with offset and limit."
    )
    # A single file shares its key with Read: printing it after a denied Read counts as the retry.
    subject = _file_subject(files[0]) if len(files) == 1 else "files:" + "|".join(sorted(map(_file_subject, files)))
    items: list[str] = []
    if sizes[largest] <= _MAX_FILE_BYTES:
        try:
            items = outline(largest.read_text(encoding="utf-8", errors="replace"), largest.suffix.lower())
        except OSError:
            pass
        if items:
            message += f" Outline of {largest.name}:"
    return _guard_once(
        session, cfg, "shell_read", subject, details, message,
        items, "If you really need all of it, repeat the same command once.",
    )


def _file_subject(path: Path) -> str:
    return "file:" + os.path.normcase(os.path.abspath(path))


def _guard_once(
    session: str, cfg: dict[str, Any], kind: str, subject: str, details: dict[str, Any],
    message: str, items: list[str], retry_hint: str,
) -> dict[str, Any] | None:
    """Coach, or deny once per subject and session. A repeated attempt means the content is needed."""
    if cfg["read_guard"] == "coach":
        log_event(kind, session, **details)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}}
    key = hashlib.sha1(subject.encode("utf-8", "replace")).hexdigest()[:16]
    state = load_state(session)
    if key in state.get("allowed_reads", []):
        return None
    blocked = state.setdefault("blocked_reads", [])
    if key in blocked:
        blocked.remove(key)
        state.setdefault("allowed_reads", []).append(key)
        save_state(session, state)
        log_event(f"{kind}_retried", session, **details)
        return None
    blocked.append(key)
    save_state(session, state)
    log_event(f"{kind}_blocked", session, **details)
    if items:
        message += "\nOutline (line: declaration):\n" + "\n".join(items)
    message += "\n" + retry_hint
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }


def _rewrite_message(path: str, stats: dict[str, int], via: str) -> str:
    share = round(100 * stats["unchanged"] / stats["new_lines"])
    return (
        f"[token guard] This {via} rewrites the existing file {path} ({stats['old_lines']} lines, "
        f"about {share}% of the new content is unchanged). The full content stays in the context and is "
        "re-read on every following call. For further changes to existing files, edit only the lines that change."
    )


# ------------------------------------------------------------------ handlers


def agent_dirs(cwd: str | None) -> list[Path]:
    dirs = [Path.home() / ".claude" / "agents"]
    return dirs + [Path(cwd) / ".claude" / "agents"] if cwd else dirs


def routing_available(cwd: str | None) -> bool:
    return any((d / "scout.md").is_file() for d in agent_dirs(cwd))


def map_guidance(map_file: Path) -> str:
    """What Claude is told about the project map. Only paths and how to use them: no map content enters the context."""
    return (
        f"Project map: {map_file.as_posix()} (layout, scripts, pages, routes). Read it before exploring instead of "
        "listing directories. To find a page, route or symbol, grep project-index.md in the same folder "
        "(name  file:line  kind). Its `deps` rows show what imports a file; `cc-optimizer impact <file>` shows what "
        "a change affects."
    )


def _announce_late(data: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Everything SessionStart gives (rules, map, work log), once per session: for a session that was already open
    when the guards were installed (SessionStart never ran for it), at its first prompt. None when it already
    ran or there is nothing to say."""
    session = str(data.get("session_id") or "unknown")
    state = load_state(session)
    if state.get("map_announced"):
        return None
    state["map_announced"] = True  # also when there is nothing to say: do not look again on every prompt
    save_state(session, state)
    parts = _start_parts(data, cfg)
    if not parts:
        return None
    log_event("map_announced", session, late=True)
    return "\n".join(parts)


def _start_parts(data: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    parts = [SESSION_GUIDANCE] if cfg["session_guidance"] else []
    if cfg["model_routing"] and routing_available(data.get("cwd")):
        parts.append(ROUTING_RULES)
    if cfg["history"]:
        from .history import maybe_sync_in_background

        maybe_sync_in_background()
    if cfg["project_map"] and data.get("cwd"):
        from .project_map import ensure_map

        map_file = ensure_map(Path(data["cwd"]))
        if map_file:
            parts.append(map_guidance(map_file))
    if cfg["worklog"] and data.get("cwd"):
        from .project_map import is_project
        from .worklog import context_text, projects_root, update

        project = Path(data["cwd"])
        if is_project(project):
            update(project, projects_root(), exclude_session=data.get("session_id"))
            log_text = context_text(project, data.get("session_id"), data.get("transcript_path"),
                                    data.get("source"), int(cfg["worklog_recent"]))
            if log_text:
                parts.append(log_text)
    return parts


def session_start(data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    session = str(data.get("session_id") or "unknown")
    state = load_state(session)
    state["map_announced"] = True  # compaction or resume start again, so a later prompt need not
    save_state(session, state)
    parts = _start_parts(data, cfg)
    if not parts:
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(parts)}}


WORKLOG_MIN_INTERVAL = 30.0  # seconds between work log writes for one session (Stop fires after every reply)


def worklog_sync(data: dict[str, Any], cfg: dict[str, Any], final: bool = False) -> dict[str, Any] | None:
    """Stop / SessionEnd: write the running session into the work log, so it does not wait for /compact."""
    if not cfg["worklog"] or not data.get("cwd"):
        return None
    from .project_map import is_project
    from .worklog import projects_root, update

    project = Path(data["cwd"])
    if not is_project(project):
        return None
    session = str(data.get("session_id") or "unknown")
    state = load_state(session)
    now = time.time()
    if not final and now - float(state.get("worklog_at", 0)) < WORKLOG_MIN_INTERVAL:
        return None
    state["worklog_at"] = now
    save_state(session, state)
    update(project, projects_root())
    return None


def worklog_end(data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    return worklog_sync(data, cfg, final=True)


NL = chr(10)


def user_prompt_submit(data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    result = _prompt_guards(data, cfg)
    note = _announce_late(data, cfg)
    age = _age_warning(data, cfg)
    if not note and not age:
        return result
    if result is None:
        result = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}
    out = result.setdefault("hookSpecificOutput", {"hookEventName": "UserPromptSubmit"})
    extra = [t for t in (note, age and age["additionalContext"], out.get("additionalContext")) if t]
    out["additionalContext"] = NL.join(extra)
    if age:
        result["systemMessage"] = NL.join(t for t in (result.get("systemMessage"), age["systemMessage"]) if t)
    return result


def session_age_hours(transcript_path: str | None) -> float | None:
    """Hours since the first record of the session transcript."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    stamp = json.loads(line).get("timestamp")
                except ValueError:
                    continue
                if isinstance(stamp, str):
                    started = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    return (datetime.now(timezone.utc) - started).total_seconds() / 3600
    except (OSError, ValueError):
        pass
    return None


def _age_warning(data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, str] | None:
    """A session left open for many hours: most of the usage limit goes to such sessions."""
    limit = float(cfg["session_age_hours"])
    if limit <= 0:
        return None
    age = session_age_hours(data.get("transcript_path"))
    if age is None or age < limit:
        return None
    session = str(data.get("session_id") or "unknown")
    band = int((age - limit) // max(float(cfg["session_age_step_hours"]), 0.5))
    state = load_state(session)
    if band <= state.get("age_band", -1):
        return None
    state["age_band"] = band
    save_state(session, state)
    log_event("session_age_warning", session, hours=round(age, 1))
    hours = f"{age:.0f}"
    return {
        "systemMessage": L(
            f"This session has been open for {hours} hours. Long sessions carry a large context into every call "
            "and use up the usage limit fastest. Consider asking for a summary and starting a new session.",
            f"Bu oturum {hours} saattir açık. Uzun oturumlar her çağrıya büyük bir bağlam taşır ve kullanım limitini "
            "en hızlı tüketir. Özet isteyip yeni bir oturum açmayı düşünün."),
        "additionalContext": (
            f"[token guard] This session has been open for about {hours} hours. When the current request is done, "
            "suggest in one sentence that the user lets you write a short summary of the state and open a new session."),
    }


def _prompt_guards(data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    session = str(data.get("session_id") or "unknown")
    context = last_context_tokens(data.get("transcript_path"))
    state = load_state(session)
    if not state.get("seen"):
        # First prompt this session: the only point where a model switch is cheap.
        state["seen"] = True
        save_state(session, state)
        if cfg["model_advice"] and (context or 0) < int(cfg["model_advice_max_context"]):
            log_event("model_advice", session, context_tokens=context or 0)
            return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": MODEL_ADVICE}}
    if context is None:
        return None
    warn, step = int(cfg["context_warn_tokens"]), max(int(cfg["context_step_tokens"]), 1)
    if context < warn:
        if state.get("context_band", -1) != -1:
            state["context_band"] = -1  # compacted or cleared: warn again next time
            save_state(session, state)
        return None
    band = (context - warn) // step
    if band <= state.get("context_band", -1):
        return None
    state["context_band"] = band
    save_state(session, state)
    log_event("context_warning", session, context_tokens=context)
    return {
        "systemMessage": L(
            f"Context is {_k(context)} tokens. Every new message makes the model read all of it again. "
            "If the topic changed, consider /clear; if you are continuing the same work, consider /compact.",
            f"Bağlam {_k(context)} token. Her yeni mesaj bu bağlamın tamamını yeniden okutuyor. "
            "Konu değiştiyse /clear, aynı işe devam ediyorsanız /compact düşünün.",
        ),
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"[token guard] The conversation context is about {_k(context)} tokens. Keep this turn focused and "
                "avoid reading large files or printing long outputs unless needed. When this request is done, "
                "suggest in one sentence that the user runs /compact, or /clear if the next task is unrelated."
            ),
        },
    }


EXPLORE_TOOLS = {"Read", "Grep", "Glob"}
WORK_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Agent", "Task"}
NUDGE_GIVE_UP = 3  # nudges ignored in a session before nudging stops

ROUTING_NUDGE = (
    "[model routing] This is call #{n} in a row that only searches or reads. If more exploration is needed, "
    "delegate it to the scout subagent (Haiku, read-only) with one precise question: its findings come back as a "
    "short report instead of staying in this context for every later call. Continue here only if the next step "
    "needs this conversation's context."
)


def _exploration(tool: str | None, inp: dict[str, Any]) -> bool | None:
    """True for a search or read, False for real work (ends a run), None for anything else."""
    if tool in EXPLORE_TOOLS:
        return True
    if tool in WORK_TOOLS:
        return False
    if tool in ("Bash", "PowerShell") and isinstance(inp.get("command"), str):
        return is_exploration(inp["command"], powershell=tool == "PowerShell")
    return None


def routing_nudge(data: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Suggest the scout subagent when the main conversation keeps exploring by itself.

    Subagents' own tool calls carry agent_id and are left alone."""
    tool = data.get("tool_name")
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if not cfg["model_routing"] or data.get("agent_id"):
        return None
    kind = _exploration(tool, inp)
    if kind is None:
        return None
    session = str(data.get("session_id") or "unknown")
    state = load_state(session)
    if tool in ("Agent", "Task"):
        log_event("subagent_call", session, subagent_type=inp.get("subagent_type") or "general-purpose",
                  model=inp.get("model") or "default", after_nudge=bool(state.get("nudged_run")))
    if not kind:
        if state.get("nudged_run") and tool not in ("Agent", "Task"):
            state["nudges_ignored"] = state.get("nudges_ignored", 0) + 1
        if state.get("explore_run") or state.get("nudged_run"):
            state.update(explore_run=0, nudged_run=False)
            save_state(session, state)
        return None
    run = state.get("explore_run", 0) + 1
    state["explore_run"] = run
    nudge = (run == int(cfg["routing_nudge_after"]) + 1 and state.get("nudges_ignored", 0) < NUDGE_GIVE_UP
             and routing_available(data.get("cwd")))
    if nudge:
        state["nudged_run"] = True
        log_event("routing_nudge", session, run=run)
    save_state(session, state)
    return ROUTING_NUDGE.format(n=run) if nudge else None


def pre_tool_use(data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    nudge = routing_nudge(data, cfg)
    result = _tool_guards(data, cfg)
    if not nudge:
        return result
    if not result:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": nudge}}
    hso = result["hookSpecificOutput"]
    if "additionalContext" in hso:
        hso["additionalContext"] += "\n" + nudge
    return result  # a denied call does not run, so the nudge can wait


def _tool_guards(data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    tool = data.get("tool_name")
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    session = str(data.get("session_id") or "unknown")
    cwd = data.get("cwd")
    min_lines = int(cfg["write_min_lines"])
    ratio = float(cfg["write_unchanged_ratio"])

    if tool == "Write" and cfg["write_guard"] in ("coach", "block"):
        path, content = inp.get("file_path"), inp.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return None
        stats = rewrite_stats(_resolve(path, cwd), content, min_lines)
        if not stats or stats["unchanged"] / stats["new_lines"] < ratio:
            return None
        message = _rewrite_message(path, stats, "Write")
        if cfg["write_guard"] == "block":
            key = hashlib.sha1(f"{path}\0{content}".encode("utf-8", "replace")).hexdigest()[:16]
            state = load_state(session)
            allowed = state.setdefault("allowed_rewrites", [])
            if key in allowed:
                allowed.remove(key)
                save_state(session, state)
                log_event("write_rewrite_retried", session, path=path, **stats)
                return None
            allowed.append(key)
            save_state(session, state)
            log_event("write_rewrite_blocked", session, path=path, **stats)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message
                    + " Use Edit for the changed parts. If a full rewrite is really needed, repeat the same Write once.",
                }
            }
        log_event("write_rewrite", session, path=path, **stats)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}}

    if tool == "Read" and cfg["read_guard"] in ("coach", "block"):
        return _large_read(inp, session, cwd, cfg)

    if tool in ("Bash", "PowerShell"):
        command = inp.get("command")
        if not isinstance(command, str):
            return None
        for target, body in heredoc_writes(command) if cfg["heredoc_guard"] and "<<" in command else []:
            stats = rewrite_stats(_resolve(target, cwd), body, min_lines)
            if stats and stats["unchanged"] / stats["new_lines"] >= ratio:
                log_event("heredoc_rewrite", session, path=target, **stats)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": _rewrite_message(target, stats, "shell heredoc"),
                    }
                }
        if cfg["read_guard"] in ("coach", "block"):
            return _shell_read(command, tool, session, cwd, cfg)
    return None


HANDLERS = {
    "session-start": session_start,
    "user-prompt-submit": user_prompt_submit,
    "pre-tool-use": pre_tool_use,
    "stop": worklog_sync,
    "session-end": worklog_end,
}


def run(event: str, stdin_text: str) -> str:
    """Entry point for `cc-optimizer hook <event>`. Always returns printable output, never raises."""
    try:
        heartbeat(event)
        data = json.loads(stdin_text) if stdin_text.strip() else {}
        if not isinstance(data, dict):
            return ""
        result = HANDLERS[event](data, load_config())
        return json.dumps(result, ensure_ascii=False) if result else ""
    except Exception:  # a broken guard must never break the session
        return ""
