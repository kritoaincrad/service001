from __future__ import annotations

import subprocess
import sys

from cc_optimizer import cli, hooks, installer


def test_every_event_the_installer_registers_is_accepted_by_the_command_line():
    registered = {
        group_hook["args"][-1]
        for groups in installer.build_hooks(sys.executable).values()
        for group in groups
        for group_hook in group["hooks"]
    }
    assert registered == set(hooks.HANDLERS) == set(cli.HOOK_EVENTS)


def test_hook_command_runs_for_each_event(tmp_path):
    """The exact command Claude Code runs, for every event: it must not fail with a usage error."""
    for event in cli.HOOK_EVENTS:
        proc = subprocess.run([sys.executable, "-m", "cc_optimizer", "hook", event], input=b"{}", capture_output=True,
                              timeout=30, cwd=tmp_path, env={**installer.clean_env(), "CC_OPTIMIZER_NO_HEARTBEAT": "1",
                                                              "CC_OPTIMIZER_HOME": str(tmp_path / "home")})
        assert proc.returncode == 0, (event, proc.stderr.decode("utf-8", "replace"))
        assert b"usage:" not in proc.stderr, event


def test_hooks_run_without_a_console_window_on_windows(tmp_path):
    import os

    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    (scripts / "pythonw.exe").write_text("", encoding="utf-8")
    python = str(scripts / "python.exe")
    expected = str(scripts / "pythonw.exe") if os.name == "nt" else python
    assert installer.windowless(python) == expected
    assert installer.build_hooks(python)["Stop"][0]["hooks"][0]["command"] == expected
    assert installer.console(str(scripts / "pythonw.exe")) == python
    assert installer.windowless("python") == "python"  # a bare name is left alone
