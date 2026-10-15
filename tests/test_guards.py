from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_optimizer import hooks, installer, statusline
from cc_optimizer.guard_config import DEFAULTS


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def cfg(**overrides):
    # Routing nudges depend on the installed agents; tests that want them turn them on. The SessionStart content
    # that a late first prompt carries (test_announce.py) is off, so these tests see only the guards.
    base = {"model_routing": False, "session_guidance": False, "project_map": False, "worklog": False, "history": False}
    return {**DEFAULTS, **base, **overrides}


def events(home: Path) -> list[dict]:
    path = home / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def transcript(tmp_path: Path, contexts: list[int], sidechain_last: int | None = None) -> Path:
    lines = []
    for ctx in contexts:
        lines.append({"type": "assistant", "message": {"model": "m", "usage": {
            "input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx - 1, "output_tokens": 5}}})
        lines.append({"type": "user", "message": {"content": "x" * 1000}})
    if sidechain_last:
        lines.append({"type": "assistant", "isSidechain": True, "message": {"model": "m", "usage": {
            "input_tokens": sidechain_last, "output_tokens": 1}}})
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


EXISTING = "\n".join(f"line {i} of the component" for i in range(200)) + "\n"


# --------------------------------------------------------------- context


def test_last_context_tokens_reads_main_thread_from_the_end(tmp_path):
    path = transcript(tmp_path, [50_000, 120_000], sidechain_last=999)
    assert hooks.last_context_tokens(str(path)) == 120_000
    assert hooks.last_context_tokens(str(tmp_path / "missing.jsonl")) is None


def test_context_warning_once_per_band_and_reset_after_compaction(tmp_path, isolated_home):
    data = {"session_id": "s1", "transcript_path": str(transcript(tmp_path, [320_000]))}
    first = hooks.user_prompt_submit(data, cfg())
    assert "320K" in first["systemMessage"]
    assert first["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert hooks.user_prompt_submit(data, cfg()) is None  # same band: silent

    data["transcript_path"] = str(transcript(tmp_path, [410_000]))
    assert hooks.user_prompt_submit(data, cfg()) is not None  # next band

    data["transcript_path"] = str(transcript(tmp_path, [30_000]))
    assert hooks.user_prompt_submit(data, cfg()) is None  # compacted: state reset
    data["transcript_path"] = str(transcript(tmp_path, [310_000]))
    assert hooks.user_prompt_submit(data, cfg()) is not None
    assert [e["kind"] for e in events(isolated_home)] == ["context_warning"] * 3


def test_no_warning_below_threshold(tmp_path):
    data = {"session_id": "s", "transcript_path": str(transcript(tmp_path, [100_000]))}
    assert hooks.user_prompt_submit(data, cfg()) is None


# --------------------------------------------------------------- writes


def test_write_rewrite_of_existing_file_is_coached(tmp_path, isolated_home):
    target = tmp_path / "Component.tsx"
    target.write_text(EXISTING, encoding="utf-8")
    new = EXISTING.replace("line 5 of", "LINE FIVE of")
    data = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Write",
            "tool_input": {"file_path": "Component.tsx", "content": new}}

    out = hooks.pre_tool_use(data, cfg())
    hso = out["hookSpecificOutput"]
    assert "permissionDecision" not in hso  # coaching never touches permissions
    assert "Component.tsx" in hso["additionalContext"] and "200 lines" in hso["additionalContext"]
    assert events(isolated_home)[0]["kind"] == "write_rewrite"


@pytest.mark.parametrize(
    "content",
    [
        "\n".join(f"completely new {i}" for i in range(200)),  # genuinely new content
    ],
)
def test_write_with_new_content_is_left_alone(tmp_path, content):
    target = tmp_path / "a.py"
    target.write_text(EXISTING, encoding="utf-8")
    data = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": content}}
    assert hooks.pre_tool_use(data, cfg()) is None


def test_shrinking_a_large_file_is_left_alone(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text(EXISTING, encoding="utf-8")
    short = "\n".join(EXISTING.splitlines()[:3])
    data = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": short}}
    assert hooks.pre_tool_use(data, cfg()) is None
    command = f"cat > {target.as_posix()} <<'EOF'\n{short}\nEOF"
    assert hooks.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": command}}, cfg()) is None


def test_small_or_new_files_are_left_alone(tmp_path):
    small = tmp_path / "small.py"
    small.write_text("a\nb\nc\n", encoding="utf-8")
    for path in (small, tmp_path / "new.py"):
        data = {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": "a\nb\nc\n"}}
        assert hooks.pre_tool_use(data, cfg()) is None


def test_block_mode_denies_once_then_allows_retry(tmp_path, isolated_home):
    target = tmp_path / "a.py"
    target.write_text(EXISTING, encoding="utf-8")
    data = {"session_id": "s", "tool_name": "Write", "tool_input": {"file_path": str(target), "content": EXISTING}}

    first = hooks.pre_tool_use(data, cfg(write_guard="block"))
    assert first["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "repeat the same Write once" in first["hookSpecificOutput"]["permissionDecisionReason"]
    assert hooks.pre_tool_use(data, cfg(write_guard="block")) is None
    assert [e["kind"] for e in events(isolated_home)] == ["write_rewrite_blocked", "write_rewrite_retried"]


def test_write_guard_off(tmp_path):
    target = tmp_path / "a.py"
    target.write_text(EXISTING, encoding="utf-8")
    data = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": EXISTING}}
    assert hooks.pre_tool_use(data, cfg(write_guard="off")) is None


# --------------------------------------------------------------- heredocs


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat > out.txt <<'EOF'\nhello\nEOF", [("out.txt", "hello")]),
        ('cat <<"EOF" > "dir/out.txt"\na\nb\nEOF\necho done', [("dir/out.txt", "a\nb")]),
        ("tee -a log.txt <<EOF\nx\nEOF", [("log.txt", "x")]),
        ("cat >> append.txt <<EOF\nx\nEOF", []),
        ("python - <<'PY'\nprint(1)\nPY", []),
        ("cat > $S/x.txt <<EOF\nx\nEOF", []),
        ("cat 2> err.txt <<EOF\nx\nEOF", []),
    ],
)
def test_heredoc_writes(command, expected):
    assert hooks.heredoc_writes(command) == expected


def test_heredoc_rewrite_is_coached(tmp_path, isolated_home):
    target = tmp_path / "big.py"
    target.write_text(EXISTING, encoding="utf-8")
    command = f"cd {tmp_path} && cat > big.py <<'EOF'\n{EXISTING}EOF"
    data = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": command}}
    out = hooks.pre_tool_use(data, cfg())
    assert "shell heredoc" in out["hookSpecificOutput"]["additionalContext"]
    assert events(isolated_home)[0]["kind"] == "heredoc_rewrite"


# --------------------------------------------------------------- entry points


def test_run_never_raises_and_session_start():
    assert hooks.run("pre-tool-use", "not json") == ""
    assert hooks.run("pre-tool-use", "") == ""
    out = json.loads(hooks.run("session-start", "{}"))
    assert "targeted edits" in out["hookSpecificOutput"]["additionalContext"]


def test_statusline():
    data = {"model": {"display_name": "Opus"}, "cost": {"total_cost_usd": 1.5},
            "context_window": {"context_window_size": 1_000_000,
                               "current_usage": {"input_tokens": 10, "cache_read_input_tokens": 449_990}}}
    line = statusline.render(data, cfg())
    assert "Opus" in line and "450K/1.0M (%45)" in line and "$1.50" in line
    assert "\033[33m" in line  # between warn and warn + 2 steps: yellow
    assert statusline.run("garbage") == ""


# --------------------------------------------------------------- installer


def test_install_merges_and_uninstall_restores(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    original = {
        "model": "opus",
        "statusLine": {"type": "command", "command": "my-status"},
        "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "prettier"}]}]},
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    installer.install(path, python="C:/py/python.exe", statusline=True, compact_window=400_000, bash_output_max=12_000)
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["bashOutputMaxChars"] == 12_000
    assert settings["model"] == "opus"
    assert settings["autoCompactWindow"] == 400_000
    assert "cc_optimizer statusline" in settings["statusLine"]["command"]
    pre = settings["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "prettier"  # existing hook kept
    assert pre[1]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit|Read|Grep|Glob|Bash|PowerShell|Agent|Task"
    assert pre[1]["hooks"][0]["args"] == ["-m", "cc_optimizer", "hook", "pre-tool-use"]
    assert list(path.parent.glob("settings.json.bak-*"))

    installer.install(path, python="C:/py/python.exe")  # reinstall does not duplicate
    assert len(json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]) == 2

    installer.uninstall(path)
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_install_refuses_invalid_json(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(installer.InstallError):
        installer.install(path)
    assert path.read_text(encoding="utf-8") == "{ broken"


def test_install_dry_run_writes_nothing(tmp_path):
    path = tmp_path / "settings.json"
    settings, _ = installer.install(path, python="py", dry_run=True)
    assert "hooks" in settings and not path.exists()


# --------------------------------------------------------------- reads

BIG_CODE = "".join(
    f"def handler_{i}(request):\n" + "".join(f"    value_{j} = compute({i}, {j})  # padding\n" for j in range(12))
    for i in range(60)
)


def test_large_read_is_denied_once_with_outline(tmp_path, isolated_home):
    target = tmp_path / "service.py"
    target.write_text(BIG_CODE, encoding="utf-8")
    data = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Read", "tool_input": {"file_path": "service.py"}}

    first = hooks.pre_tool_use(data, cfg())["hookSpecificOutput"]
    assert first["permissionDecision"] == "deny"
    reason = first["permissionDecisionReason"]
    assert "780 lines" in reason and "offset and limit" in reason
    assert "1: def handler_0(request):" in reason and "14: def handler_1(request):" in reason
    assert reason.count("def handler_") == 40  # outline is capped

    assert hooks.pre_tool_use(data, cfg()) is None  # retry: the whole file is needed
    assert hooks.pre_tool_use(data, cfg()) is None  # and not asked again in this session
    assert [e["kind"] for e in events(isolated_home)] == ["read_large_blocked", "read_large_retried"]


def test_ranged_small_and_binary_reads_are_left_alone(tmp_path):
    big = tmp_path / "big.py"
    big.write_text(BIG_CODE, encoding="utf-8")
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG" + b"0" * 50_000)
    small = tmp_path / "small.py"
    small.write_text("x = 1\n", encoding="utf-8")
    for inp in (
        {"file_path": str(big), "offset": 100, "limit": 50},
        {"file_path": str(big), "limit": 200},
        {"file_path": str(image)},
        {"file_path": str(small)},
        {"file_path": str(tmp_path / "missing.py")},
    ):
        assert hooks.pre_tool_use({"tool_name": "Read", "tool_input": inp}, cfg()) is None


def test_read_guard_coach_and_off(tmp_path, isolated_home):
    target = tmp_path / "notes.md"
    target.write_text("# Title\n" + "text line that pads the file\n" * 800 + "## Section\n", encoding="utf-8")
    data = {"tool_name": "Read", "tool_input": {"file_path": str(target)}}
    coached = hooks.pre_tool_use(data, cfg(read_guard="coach"))["hookSpecificOutput"]
    assert "permissionDecision" not in coached and "Grep" in coached["additionalContext"]
    assert hooks.pre_tool_use(data, cfg(read_guard="off")) is None
    assert hooks.outline(target.read_text(encoding="utf-8"), ".md") == ["1: # Title", "802: ## Section"]


# --------------------------------------------------------------- shell file prints

from cc_optimizer.shell_reads import file_prints, shell_path  # noqa: E402


@pytest.fixture
def files(tmp_path):
    (tmp_path / "src").mkdir()
    for name in ("a.py", "b.py", "src/c.py"):
        (tmp_path / name).write_text(BIG_CODE, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat a.py b.py", ["a.py", "b.py"]),
        ("cd src && cat c.py; echo done", ["src/c.py"]),
        ('cat -n "a.py" 2>/dev/null', ["a.py"]),
        ("cat a.py | head -50", []),
        ("cat a.py > copy.py", []),
        ("cat a.py >> log.txt", []),
        ("sed -n 1,80p a.py && head -n 20 b.py", []),
        ("cat $F *.py missing.py", []),
        ("cat <<EOF\nx\nEOF", []),
        ("grep -n handler a.py", []),
    ],
)
def test_bash_file_prints(files, command, expected):
    assert file_prints(command, str(files)) == [files / e for e in expected]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("Get-Content a.py", ["a.py"]),
        ("gc -Raw -Encoding utf8 'src\\c.py'", ["src/c.py"]),
        ("Set-Location src; type c.py", ["src/c.py"]),
        ("Get-Content a.py -TotalCount 40", []),
        ("Get-Content a.py | Select-Object -First 20", []),
    ],
)
def test_powershell_file_prints(files, command, expected):
    assert file_prints(command, str(files), powershell=True) == [files / e for e in expected]


def test_git_bash_drive_paths():
    assert shell_path("/c/Users/x", None).as_posix() == "C:/Users/x"


def test_shell_print_of_large_files_is_denied_once(files, isolated_home):
    data = {"session_id": "s", "cwd": str(files), "tool_name": "Bash", "tool_input": {"command": "cat a.py b.py"}}
    reason = hooks.pre_tool_use(data, cfg())["hookSpecificOutput"]["permissionDecisionReason"]
    assert "a.py, b.py" in reason and "sed -n" in reason and "Outline of a.py" in reason
    assert hooks.pre_tool_use(data, cfg()) is None
    small = {"tool_name": "Bash", "cwd": str(files), "tool_input": {"command": "cat a.py"}}
    assert hooks.pre_tool_use(small, cfg(read_guard_tokens=50_000)) is None
    assert [e["kind"] for e in events(isolated_home)] == ["shell_read_blocked", "shell_read_retried"]


def test_cat_after_denied_read_counts_as_retry(files, isolated_home):
    read = {"session_id": "s", "cwd": str(files), "tool_name": "Read", "tool_input": {"file_path": "a.py"}}
    cat = {"session_id": "s", "cwd": str(files), "tool_name": "Bash", "tool_input": {"command": "cat ./a.py"}}
    assert hooks.pre_tool_use(read, cfg())["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hooks.pre_tool_use(cat, cfg()) is None
    assert hooks.pre_tool_use(read, cfg()) is None


def test_install_rejects_out_of_range_values(tmp_path):
    with pytest.raises(installer.InstallError):
        installer.install(tmp_path / "s.json", bash_output_max=500, dry_run=True)


# --------------------------------------------------------------- model routing


def test_model_advice_only_on_the_first_prompt(tmp_path, isolated_home):
    data = {"session_id": "new", "transcript_path": str(tmp_path / "not-yet.jsonl")}
    first = hooks.user_prompt_submit(data, cfg())
    assert "/model haiku" in first["hookSpecificOutput"]["additionalContext"]
    assert hooks.user_prompt_submit(data, cfg()) is None
    late = {"session_id": "old", "transcript_path": str(transcript(tmp_path, [200_000]))}
    assert hooks.user_prompt_submit(late, cfg()) is None  # installed mid-session: switching is no longer cheap
    off = {"session_id": "off", "transcript_path": data["transcript_path"]}
    assert hooks.user_prompt_submit(off, cfg(model_advice=False)) is None
    assert [e["kind"] for e in events(isolated_home)] == ["model_advice"]


def test_routing_rules_only_when_agents_are_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.Path, "home", lambda: tmp_path / "nohome")
    text = lambda: hooks.session_start({"cwd": str(tmp_path)}, cfg(session_guidance=True, model_routing=True))["hookSpecificOutput"]["additionalContext"]
    assert "scout" not in text()
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "scout.md").write_text("x", encoding="utf-8")
    assert "scout (Haiku)" in text() and "targeted edits" in text()
    assert hooks.session_start({"cwd": str(tmp_path)}, cfg(session_guidance=False, model_routing=False)) is None


def test_install_agents_and_subagent_model(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    original = {"model": "opus", "env": {"FOO": "1"}}
    path.write_text(json.dumps(original), encoding="utf-8")
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir()
    (agents / "expert.md").write_text("my own expert", encoding="utf-8")

    _, notes = installer.install(path, python="py", agents=True, subagent_model="sonnet", model="sonnet")
    assert (agents / "scout.md").read_text(encoding="utf-8").count("model: haiku") == 1
    assert "model: sonnet" in (agents / "worker.md").read_text(encoding="utf-8")
    assert "model: opus" in (agents / "architect.md").read_text(encoding="utf-8")
    assert (agents / "expert.md").read_text(encoding="utf-8") == "my own expert"  # not clobbered
    assert any("expert" in n for n in notes)
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["env"] == {"FOO": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}
    assert settings["model"] == "sonnet"

    # Reinstall keeps the first record, so uninstall restores the state before the first install.
    installer.install(path, python="py", agents=True, subagent_model="haiku", model="haiku")
    installer.uninstall(path)
    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert sorted(p.name for p in agents.iterdir()) == ["expert.md"]
    with pytest.raises(installer.InstallError):
        installer.install(path, subagent_model="gpt", dry_run=True)
