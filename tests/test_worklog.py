from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cc_optimizer import hooks, worklog
from cc_optimizer.guard_config import DEFAULTS
from cc_optimizer.transcripts import project_dir_name


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path / "home"))


def prompt(text: str, ts: str, cwd: str) -> dict:
    return {"type": "user", "timestamp": ts, "cwd": cwd, "message": {"content": [{"type": "text", "text": text}]}}


def reply(blocks: list[dict], ts: str, cwd: str) -> dict:
    return {"type": "assistant", "timestamp": ts, "cwd": cwd, "message": {"content": blocks}}


def tool(name: str, **inp) -> dict:
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def session(project: Path, day: str, title: str, request: str, edited: str) -> list[dict]:
    cwd = str(project)
    ts = f"2026-10-{day}T09:00:00Z"
    return [
        {"type": "ai-title", "aiTitle": title},
        prompt("<ide_opened_file>The user opened a file</ide_opened_file>", ts, cwd),
        prompt(f'{request} <pasted_content id="ab12">\n[API] error 500\n</pasted_content id="ab12">', ts, cwd),
        reply([tool("Edit", file_path=str(project / edited), old_string="a", new_string="b"),
               tool("Edit", file_path=str(project / edited), old_string="b", new_string="c"),
               tool("Write", file_path="C:/Temp/scratch/probe.py", content="x"),
               tool("Bash", command=f'cd "{cwd}" && npm test -- --watch=false'),
               tool("Bash", command="cat src/a.ts"),
               tool("Bash", command="for f in *.ts; do echo $f; done")], ts, cwd),
        {"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "subagent text"}]}},
        reply([{"type": "text", "text": "Done: the login error is fixed and the tests pass."}], ts, cwd),
    ]


def write(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path):
    project = tmp_path / "shop"
    (project / ".git" / "info").mkdir(parents=True)
    (project / "package.json").write_text("{}", encoding="utf-8")
    root = tmp_path / "projects"
    folder = root / project_dir_name(project)
    write(folder / "s1.jsonl", session(project, "10", "Login fix", "fix the login error", "src/auth.ts"))
    write(folder / "s2.jsonl", session(project, "12", "Navbar colors", "make the navbar green", "src/navbar.tsx"))
    return project, root, folder


def test_extract_keeps_only_real_work(setup):
    project, _, folder = setup
    entry = worklog.extract(folder / "s1.jsonl")
    assert entry["prompts"] == ["fix the login error [pasted]"]
    assert entry["title"] == "Login fix" and entry["outcome"].startswith("Done: the login error")
    assert entry["commands"] == {"npm test": 1}  # cat and the for loop only look at things
    text = worklog.render_entry(entry)
    assert "- Changed: src/auth.ts (2)" in text and "probe.py" not in text  # files outside the project
    assert "subagent text" not in text and "interrupted" not in text


def test_update_writes_newest_first_and_skips_unchanged(setup):
    project, root, folder = setup
    assert len(worklog.update(project, root)) == 2
    log = worklog.log_path(project).read_text(encoding="utf-8")
    assert log.index("Navbar colors") < log.index("Login fix")
    assert ".claude/worklog.md" in (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert worklog.update(project, root) == []  # nothing changed

    path = folder / "s1.jsonl"
    entries = session(project, "10", "Login fix", "fix the login error", "src/auth.ts")
    entries.append(prompt("also add rate limiting", "2026-10-10T10:00:00Z", str(project)))
    write(path, entries)
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert [e["session_id"] for e in worklog.update(project, root)] == ["s1"]
    log = worklog.log_path(project).read_text(encoding="utf-8")
    assert log.count("Login fix") == 1 and "also add rate limiting" in log


def test_context_text_lists_recent_and_this_session_after_compaction(setup):
    project, root, folder = setup
    worklog.update(project, root, exclude_session="s2")
    text = worklog.context_text(project, "s2", None, "startup")
    assert "worklog.md" in text and "Login fix: changed auth.ts" in text and "Navbar" not in text
    after = worklog.context_text(project, "s2", str(folder / "s2.jsonl"), "compact")
    assert "This session before compaction:" in after and "Navbar colors: changed navbar.tsx" in after


def test_session_start_adds_the_log(setup, monkeypatch):
    project, root, _ = setup
    monkeypatch.setattr(worklog, "projects_root", lambda: root)
    cfg = {**DEFAULTS, "model_routing": False, "project_map": False}
    context = hooks.session_start({"cwd": str(project), "session_id": "new", "source": "startup"}, cfg)
    text = context["hookSpecificOutput"]["additionalContext"]
    assert "Recent sessions:" in text and "Navbar colors" in text and "Login fix" in text
    off = hooks.session_start({"cwd": str(project), "session_id": "new"}, {**cfg, "worklog": False})
    assert "Recent sessions" not in off["hookSpecificOutput"]["additionalContext"]


def test_stop_logs_the_running_session_and_throttles(setup, monkeypatch):
    project, root, folder = setup
    monkeypatch.setattr(worklog, "projects_root", lambda: root)
    data = {"cwd": str(project), "session_id": "s2"}
    assert hooks.worklog_sync(data, DEFAULTS) is None
    assert "Navbar colors" in worklog.log_path(project).read_text(encoding="utf-8")
    write(folder / "s3.jsonl", session(project, "13", "Cart bug", "fix the cart", "src/cart.ts"))
    hooks.worklog_sync({**data, "session_id": "s3"}, DEFAULTS)
    hooks.worklog_sync({**data, "session_id": "s3"}, DEFAULTS)  # within the interval: skipped
    text = worklog.log_path(project).read_text(encoding="utf-8")
    assert text.count("Cart bug") == 1
    assert hooks.worklog_sync(data, {**DEFAULTS, "worklog": False}) is None
