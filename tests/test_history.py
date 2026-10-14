from __future__ import annotations

import json
import os

import pytest

from cc_optimizer import history
from cc_optimizer.cli import main
from cc_optimizer.history import maybe_sync_in_background  # before conftest replaces it
from test_cc_optimizer import assistant, sample_session, write_jsonl

GUARD_CONTEXT = {"type": "attachment", "attachment": {
    "type": "hook_additional_context", "content": ["Token efficiency rules for this session:\n- ..."]}}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path / "home"))


def make_root(tmp_path):
    root = tmp_path / "projects"
    plain = sample_session()
    guarded = [GUARD_CONTEXT] + [{**e, "timestamp": "2026-10-20T10:00:00.000Z"} if e.get("type") == "assistant" else e
                                 for e in sample_session()]
    write_jsonl(root / "C--work-KEIMORA" / "old1.jsonl", plain)
    write_jsonl(root / "C--work-KEIMORA" / "new1.jsonl", guarded)
    write_jsonl(root / "C--work-KEIMORA" / "new1" / "subagents" / "agent-a.jsonl",
                [{**assistant("x1", [{"type": "text", "text": "done"}], ctx=(5, 3_000, 0)), "isSidechain": True}])
    write_jsonl(root / "C--work-other" / "o1.jsonl", plain)
    return root


def test_sync_labels_sessions_and_keeps_rows_after_cleanup(tmp_path):
    root = make_root(tmp_path)
    con = history.connect()
    assert history.sync(root, con, fast=True) == {"transcripts": 3, "updated": 3, "unchanged": 0}
    by_id = {r["session_id"]: r for r in history.rows(con)}
    assert by_id["new1"]["guarded"] == 1 and by_id["old1"]["guarded"] == 0
    assert by_id["new1"]["calls"] == by_id["old1"]["calls"] + 1  # subagent counted with its session
    assert json.loads(by_id["new1"]["models_sub"]) == {"claude-test": 1}
    assert by_id["old1"]["calibration"] == history.DEFAULT_CALIBRATION  # too few samples to calibrate

    assert history.sync(root, con, fast=True)["updated"] == 0  # unchanged transcripts are skipped
    path = root / "C--work-KEIMORA" / "old1.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + json.dumps(assistant("r9", [], ctx=(5, 0, 50_000))) + "\n",
                    encoding="utf-8")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    assert history.sync(root, con, fast=True)["updated"] == 1

    path.unlink()  # Claude Code cleaned the transcript up
    history.sync(root, con, fast=True)
    assert "old1" in {r["session_id"] for r in history.rows(con)}


def test_rows_filter_by_project_name(tmp_path):
    con = history.connect()
    history.sync(make_root(tmp_path), con, fast=True)
    assert {r["session_id"] for r in history.rows(con, "keimora")} == {"old1", "new1"}
    assert [r["session_id"] for r in history.rows(con, "KEIMORA", guarded=True)] == ["new1"]


def test_as_report_feeds_compare(tmp_path):
    from cc_optimizer.compare import compare

    con = history.connect()
    history.sync(make_root(tmp_path), con, fast=True)
    before = history.as_report(history.rows(con, guarded=False))
    after = history.as_report(history.rows(con, guarded=True))
    assert before["scope"]["sessions"] == 2 and after["scope"]["sessions"] == 1
    assert before["tokens"]["calls_by_model"]["main"] == {"claude-test": 12}
    rows = {r["metric"]: r for r in compare(before, after)["rows"]}
    assert rows["Çağrı başına işlenen token"]["before"] > 0
    assert 0 < rows["Yükte dosya okumalarının payı"]["before"] < 1


def test_history_and_compare_commands(tmp_path, capsys):
    root = str(make_root(tmp_path))
    assert main(["history", "--root", root, "--fast", "--weeks", "520"]) == 0
    out = capsys.readouterr().out
    assert "Kayıtta 3 oturum var" in out and "C--work-KEIMORA" in out and "1/3" in out
    assert main(["compare", "--root", root, "--fast", "--project", "KEIMORA"]) == 0
    out = capsys.readouterr().out
    assert "Service001'siz:" in out and "Service001'li:" in out and "(KEIMORA)" in out
    assert main(["compare", "--root", root, "--fast", "--project", "other"]) == 0
    assert "Service001'li 0" in capsys.readouterr().out


def test_background_sync_is_throttled(monkeypatch):
    started = []
    monkeypatch.setattr(history.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    assert maybe_sync_in_background() is True
    assert maybe_sync_in_background() is False  # within the hour
    assert started[0][-2:] == ["history", "--sync-only"]
    assert maybe_sync_in_background(interval=0) is True
    assert len(started) == 2


def test_guards_installed_mid_session_are_mixed(tmp_path):
    entries = sample_session()
    mixed = write_jsonl(tmp_path / "mixed.jsonl", entries[:4] + [GUARD_CONTEXT] + entries[4:])
    plain = write_jsonl(tmp_path / "plain.jsonl", entries)
    guarded = write_jsonl(tmp_path / "guarded.jsonl", [GUARD_CONTEXT] + entries)
    assert history.guard_label(mixed) == history.MIXED
    assert history.guard_label(plain) == history.UNGUARDED
    assert history.guard_label(guarded) == history.GUARDED
