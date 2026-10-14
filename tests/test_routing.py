from __future__ import annotations

import json

import pytest

from cc_optimizer import hooks
from cc_optimizer.guard_config import DEFAULTS


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(hooks, "routing_available", lambda cwd: True)
    return tmp_path / "home"


CFG = {**DEFAULTS, "model_routing": True, "routing_nudge_after": 4}


def call(tool, session="s", **extra):
    inputs = {"Grep": {"pattern": "x"}, "Read": {"file_path": "missing.py"}, "Glob": {"pattern": "*"},
              "Edit": {"file_path": "a.py"}, "Agent": {"subagent_type": "scout", "model": "haiku"},
              "BashRead": {"command": "cd src && grep -n foo a.py | head"}, "BashWork": {"command": "npm test"}}
    name = "Bash" if tool.startswith("Bash") else tool
    return hooks.pre_tool_use({"session_id": session, "tool_name": name, "tool_input": inputs[tool], **extra}, CFG)


def context(out):
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def events(home):
    path = home / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_nudge_after_four_explorations_once_per_run():
    outs = [call(t) for t in ("Grep", "Read", "BashRead", "Glob", "Grep", "Read")]
    assert [("scout" in context(o)) for o in outs] == [False, False, False, False, True, False]
    assert "call #5 in a row" in context(outs[4])
    call("Edit")  # real work ends the run
    assert not any("scout" in context(call(t)) for t in ("Grep",) * 4)
    assert "scout" in context(call("Grep"))


def test_work_commands_and_neutral_tools_do_not_count():
    for t in ("Grep", "Grep", "BashWork", "Grep", "Grep", "Grep"):
        out = call(t)
    assert "scout" not in context(out)  # npm test ended the first run


def test_subagent_calls_are_never_nudged_and_logged(isolated_home):
    for _ in range(6):
        assert "scout" not in context(call("Grep", agent_id="a1", agent_type="scout"))
    for t in ("Grep",) * 5:
        call(t)
    call("Agent")
    kinds = [(e["kind"], e.get("after_nudge")) for e in events(isolated_home)]
    assert kinds == [("routing_nudge", None), ("subagent_call", True)]


def test_nudging_stops_after_three_ignored_nudges():
    for _ in range(3):
        for t in ("Grep",) * 5 + ("Edit",):
            call(t)
    assert not any("scout" in context(call("Grep")) for _ in range(8))


def test_nudge_is_added_to_guard_context_and_can_be_disabled(monkeypatch):
    monkeypatch.setattr(hooks, "_tool_guards", lambda data, cfg: {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "guard says hi"}})
    outs = [call("Grep") for _ in range(5)]
    assert context(outs[4]).startswith("guard says hi\n[model routing]")
    off = hooks.pre_tool_use({"session_id": "x", "tool_name": "Grep", "tool_input": {}},
                             {**CFG, "model_routing": False})
    assert context(off) == "guard says hi"
