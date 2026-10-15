from __future__ import annotations

import json
import sys

import pytest

from cc_optimizer import cli, hooks, installer
from cc_optimizer.guard_config import DEFAULTS, load_state


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path / "home"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "shop"}), encoding="utf-8")
    (root / "src" / "main.py").write_text('"""Entry point."""\n', encoding="utf-8")
    return root


CFG = {**DEFAULTS, "model_routing": False, "worklog": False, "history": False}


def prompt(project, session="s1", cfg=CFG):
    return hooks.user_prompt_submit({"cwd": str(project), "session_id": session}, cfg)


def context_of(result):
    return (result or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def test_an_already_open_session_gets_the_map_at_its_first_prompt(project):
    first = prompt(project, cfg={**CFG, "model_advice": False})
    assert "Project map:" in context_of(first) and "project-index.md" in context_of(first)
    assert (project / ".claude" / "project-map.md").is_file() and (project / ".claude" / "project-index.md").is_file()
    assert context_of(prompt(project, cfg={**CFG, "model_advice": False})) == ""  # only once per session
    assert load_state("s1")["map_announced"] is True


def test_the_announcement_is_merged_with_the_model_advice(project):
    context = context_of(prompt(project))  # first prompt: model advice is on by default
    assert "Project map:" in context and "[model advice]" in context


def test_a_session_started_normally_is_not_told_twice(project):
    start = hooks.session_start({"cwd": str(project), "session_id": "s2"}, CFG)
    assert "Project map:" in start["hookSpecificOutput"]["additionalContext"]
    assert "Project map:" not in context_of(prompt(project, session="s2"))


def test_no_announcement_when_the_map_is_off_or_this_is_no_project(project, tmp_path):
    assert "Project map:" not in context_of(prompt(project, session="s3", cfg={**CFG, "project_map": False}))
    loose = tmp_path / "loose"
    loose.mkdir()
    assert "Project map:" not in context_of(prompt(loose, session="s4"))
    assert not (loose / ".claude").exists()
    assert load_state("s4")["map_announced"] is True  # it does not look again on every prompt


def test_install_adds_and_uninstall_removes_the_project_map_command(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    installer.install(settings, python=sys.executable.replace("\\", "/"))
    command = tmp_path / ".claude" / "commands" / "project-map.md"
    text = command.read_text(encoding="utf-8")
    assert text.startswith("---\ndescription:") and "map --announce" in text and "cc_optimizer" in text
    assert f'"{sys.executable.replace(chr(92), "/")}"' in text
    installer.uninstall(settings)
    assert not command.exists()


def test_a_command_of_your_own_is_left_alone(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    own = tmp_path / ".claude" / "commands" / "project-map.md"
    own.parent.mkdir(parents=True)
    own.write_text("my own command\n", encoding="utf-8")
    _, notes = installer.install(settings, python=sys.executable)
    assert own.read_text(encoding="utf-8") == "my own command\n"
    assert any("project-map" in n and ("untouched" in n or "dokunulmadı" in n) for n in notes)
    installer.uninstall(settings)
    assert own.exists()  # not ours: not removed


def test_map_announce_command(project, capsys):
    assert cli.main(["map", "--announce", "--project-dir", str(project)]) == 0
    out = capsys.readouterr().out
    assert "Project map:" in out and "project-index.md" in out and (project / ".claude" / "project-index.md").is_file()
    assert cli.main(["map", "--announce", "--project-dir", str(project / "nope")]) == 1


def test_an_open_session_also_gets_the_rules_and_the_work_log_at_its_first_prompt(project, tmp_path, monkeypatch):
    from cc_optimizer import worklog

    monkeypatch.setattr(worklog, "projects_root", lambda: tmp_path / "no-transcripts")
    log = project / ".claude" / "worklog.md"
    log.parent.mkdir(exist_ok=True)
    log.write_text("<!-- session:old start:2026-10-14T09:00:00Z -->\n### Login fix\n- Changed: src/auth.ts (2)\n",
                   encoding="utf-8")
    cfg = {**CFG, "worklog": True, "session_guidance": True, "model_advice": False}
    context = context_of(prompt(project, session="s5", cfg=cfg))
    assert "Token efficiency rules" in context and "Project map:" in context and "Login fix: changed auth.ts" in context
    assert context_of(prompt(project, session="s5", cfg=cfg)) == ""
