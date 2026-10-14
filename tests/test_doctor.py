from __future__ import annotations

import json
import os
import sys
import time

import pytest

from cc_optimizer import doctor, installer
from cc_optimizer.guard_config import heartbeat


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CC_OPTIMIZER_NO_HEARTBEAT", raising=False)
    return tmp_path / "home"


def statuses(checks):
    return {c.title: c.status for c in checks}


def installed(tmp_path, python=sys.executable):
    path = tmp_path / ".claude" / "settings.json"
    installer.install(path, python=python, statusline=True)
    return path


def test_healthy_install_runs_the_real_hook(tmp_path, isolated_home):
    checks, python = doctor.check_settings(installed(tmp_path))
    assert python == sys.executable
    assert statuses(checks)["Hook'lar kurulu"] == doctor.OK
    runtime = statuses(doctor.check_runtime(python))
    assert runtime["Hook çalışıyor"] == doctor.OK and runtime["Durum satırı çalışıyor"] == doctor.OK
    assert not (isolated_home / "heartbeat").exists()  # the test run is not counted as activity


def test_missing_python_and_broken_or_missing_settings(tmp_path):
    _, python = doctor.check_settings(installed(tmp_path, python=str(tmp_path / "gone" / "python.exe")))
    assert statuses(doctor.check_runtime(python))["Python bulunamadı"] == doctor.FAIL
    broken = tmp_path / "broken.json"
    broken.write_text("{ nope", encoding="utf-8")
    assert doctor.check_settings(broken)[0][0].status == doctor.FAIL
    assert doctor.check_settings(tmp_path / "none.json")[0][0].status == doctor.FAIL


def test_outdated_and_missing_hooks(tmp_path):
    path = installed(tmp_path)
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["hooks"]["PreToolUse"][0]["matcher"] = "Write|Bash"
    del settings["hooks"]["UserPromptSubmit"]
    path.write_text(json.dumps(settings), encoding="utf-8")
    result = statuses(doctor.check_settings(path)[0])
    assert result["Eksik hook'lar"] == doctor.FAIL
    assert result["Hook'lar eski sürümle kurulmuş"] == doctor.WARN


def test_activity_detects_hooks_that_stopped_running(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    transcript = projects / "s.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    assert doctor.check_activity(projects)[0].status == doctor.WARN  # used, but no heartbeat at all

    heartbeat("pre-tool-use")
    assert doctor.check_activity(projects)[0].status == doctor.OK
    later = time.time() + 2 * 3600
    os.utime(transcript, (later, later))  # Claude Code used two hours after the last hook run
    assert doctor.check_activity(projects, now=later)[0].status == doctor.FAIL


def test_config_agents_and_duplicate_install(tmp_path, isolated_home):
    isolated_home.mkdir(parents=True)
    (isolated_home / "config.json").write_text(json.dumps({"read_guard": "coach", "raed_guard": 1, "project_map": "no"}),
                                               encoding="utf-8")
    config = statuses(doctor.check_config())
    assert config["config.json içinde bilinmeyen ayarlar"] == doctor.WARN
    assert config["config.json içinde türü yanlış ayarlar"] == doctor.WARN

    agents = tmp_path / "agents"
    assert doctor.check_agents(agents)[0].status == doctor.INFO
    agents.mkdir()
    for source in installer.AGENT_SOURCE.glob("*.md"):
        (agents / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    assert doctor.check_agents(agents)[0].status == doctor.OK
    (agents / "scout.md").write_text("edited", encoding="utf-8")
    assert "farklı: scout" in doctor.check_agents(agents)[0].detail

    project = tmp_path / "proj"
    installer.install(installer.settings_path("local", project), python="py")
    assert statuses(doctor.check_project(project))["Bu projede ikinci kurulum var"] == doctor.WARN


def _history_with(models, tag="a"):
    from cc_optimizer import history

    con = history.connect()
    cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
    for i, m in enumerate(models):
        row = dict.fromkeys(cols, 0)
        row.update(session_id=f"{tag}{i}", project="p", first_ts=f"2026-10-1{tag == 'b'}{i}T00:00:00Z", models_main=json.dumps({m: 10}))
        con.execute(f"INSERT INTO sessions ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    con.commit()
    con.close()


def test_model_check_flags_ignored_default(tmp_path):
    path = installed(tmp_path)
    settings = json.loads(path.read_text())
    settings["model"] = "sonnet"
    path.write_text(json.dumps(settings))
    _history_with(["claude-opus-5-5"] * 3)
    assert statuses(doctor.check_model(path))["Varsayılan model uygulanmıyor"] == doctor.WARN
    _history_with(["claude-sonnet-5-5"] * 3, "b")
    assert statuses(doctor.check_model(path))["Varsayılan model"] == doctor.OK
