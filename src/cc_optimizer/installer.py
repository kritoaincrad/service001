"""Add or remove the context guards in a Claude Code settings file.

The settings file is merged, never replaced: existing hooks and keys are kept,
a timestamped backup is written first, and values we override (statusLine,
autoCompactWindow) are recorded so that uninstall can restore them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .guard_config import home
from .i18n import L

MARKER = "cc_optimizer"
SCOPES = ("user", "project", "local")


class InstallError(Exception):
    pass


def clean_env() -> dict[str, str]:
    """The environment a hook gets from Claude Code, as far as Python is concerned: without a PYTHONPATH that only
    this terminal has. A hook that works only because of it would fail inside Claude Code."""
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}


def verify_python(python: str) -> None:
    """Raise InstallError unless `python` can import this package on its own (see clean_env)."""
    try:
        proc = subprocess.run([python, "-c", f"import {MARKER}"], capture_output=True, timeout=30, env=clean_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(L(f"{python} could not be run: {exc}", f"{python} çalıştırılamadı: {exc}")) from exc
    if proc.returncode != 0:
        raise InstallError(L(
            f"{python} cannot import {MARKER}, so the hooks would fail inside Claude Code. Nothing was written. "
            f"Run the install with the Python that has Service001 installed (for example "
            f".venv/Scripts/python -m {MARKER} install ...), or install the package into that Python first "
            f"(pip install -e .).",
            f"{python} {MARKER} paketini içe aktaramıyor, bu yüzden hook'lar Claude Code içinde hata verirdi. "
            f"Hiçbir şey yazılmadı. Kurulumu Service001'in kurulu olduğu Python ile çalıştırın (örneğin "
            f".venv/Scripts/python -m {MARKER} install ...) ya da paketi önce o Python'a kurun (pip install -e .).")) from None


def settings_path(scope: str, project_dir: str | Path | None = None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    base = Path(project_dir or Path.cwd())
    return base / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")


def windowless(python: str) -> str:
    """On Windows, python.exe opens a console window for every hook call; the pythonw.exe next to it does not
    (it still reads stdin and writes stdout when they are pipes, as they are for hooks and the status line)."""
    path = Path(python)
    if os.name == "nt" and path.name.lower() == "python.exe" and path.with_name("pythonw.exe").is_file():
        return str(path.with_name("pythonw.exe"))
    return python


def console(python: str) -> str:
    """The python.exe that belongs to a pythonw.exe: it reports errors on stderr, which doctor wants to read."""
    path = Path(python)
    if path.name.lower() == "pythonw.exe" and path.with_name("python.exe").is_file():
        return str(path.with_name("python.exe"))
    return python


def _hook(python: str, event: str) -> dict[str, Any]:
    # Exec form: no shell, so paths with spaces or backslashes need no quoting.
    return {"type": "command", "command": windowless(python), "args": ["-m", MARKER, "hook", event], "timeout": 10}


def build_hooks(python: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "SessionStart": [{"matcher": "startup|clear|compact", "hooks": [_hook(python, "session-start")]}],
        "UserPromptSubmit": [{"hooks": [_hook(python, "user-prompt-submit")]}],
        "PreToolUse": [{"matcher": "Write|Edit|MultiEdit|NotebookEdit|Read|Grep|Glob|Bash|PowerShell|Agent|Task", "hooks": [_hook(python, "pre-tool-use")]}],
        "Stop": [{"hooks": [_hook(python, "stop")]}],
        "SessionEnd": [{"hooks": [_hook(python, "session-end")]}],
    }


def statusline_command(python: str) -> str:
    # statusLine has no exec form and runs through a shell.
    return f'"{Path(windowless(python)).as_posix()}" -m {MARKER} statusline'


def _is_ours(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks") or []:
        if isinstance(hook, dict):
            text = " ".join([str(hook.get("command", "")), *map(str, hook.get("args") or [])])
            if MARKER in text:
                return True
    return False


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise InstallError(L(f"{path} is not valid JSON, left untouched: {exc}", f"{path} geçerli JSON değil, dokunulmadı: {exc}")) from exc
    if not isinstance(data, dict):
        raise InstallError(L(f"{path} is not a JSON object, left untouched.", f"{path} bir JSON nesnesi değil, dokunulmadı."))
    return data


def _write(path: Path, data: dict[str, Any]) -> Path | None:
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup


def _records_path() -> Path:
    return home() / "install.json"


def _load_records() -> dict[str, Any]:
    try:
        data = json.loads(_records_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_records(records: dict[str, Any]) -> None:
    _records_path().parent.mkdir(parents=True, exist_ok=True)
    _records_path().write_text(json.dumps(records, indent=2), encoding="utf-8")


def _strip_ours(settings: dict[str, Any]) -> None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        groups = hooks[event]
        if isinstance(groups, list):
            kept = [g for g in groups if not _is_ours(g)]
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not hooks:
        del settings["hooks"]


AGENT_SOURCE = Path(__file__).parent / "agents"
SUBAGENT_MODELS = ("haiku", "sonnet", "opus", "fable", "inherit")
SUBAGENT_ENV = "CLAUDE_CODE_SUBAGENT_MODEL"


def agents_dir(settings_file: Path) -> Path:
    """~/.claude/agents for user scope, <project>/.claude/agents otherwise."""
    return settings_file.parent / "agents"


def _install_agents(path: Path, record: dict[str, Any], dry_run: bool, notes: list[str]) -> None:
    target_dir = agents_dir(path)
    ours = set(record.get("agents", []))
    written, skipped = [], []
    for source in sorted(AGENT_SOURCE.glob("*.md")):
        target = target_dir / source.name
        content = source.read_text(encoding="utf-8")
        if target.exists() and str(target) not in ours and target.read_text(encoding="utf-8") != content:
            skipped.append(source.stem)  # the user's own agent with the same name
            continue
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        ours.add(str(target))
        written.append(source.stem)
    record["agents"] = sorted(ours)
    if written:
        notes.append(L(f"Subagents installed ({target_dir}): {', '.join(written)}.",
                       f"Alt ajanlar kuruldu ({target_dir}): {', '.join(written)}."))
    if skipped:
        notes.append(L(f"Left untouched because you have your own agent with the same name: {', '.join(skipped)}.",
                       f"Aynı adda kendi ajanınız olduğu için dokunulmadı: {', '.join(skipped)}."))


COMMAND_NAME = "project-map"


def commands_dir(settings_file: Path) -> Path:
    """~/.claude/commands for user scope, <project>/.claude/commands otherwise."""
    return settings_file.parent / "commands"


def command_text(python: str) -> str:
    """The /project-map slash command: refresh the map and index in a session that is already open. The text goes to
    Claude, so it is English."""
    exe = Path(python).as_posix()
    return (
        "---\n"
        "description: Refresh the project map and index of this project and load them into this session\n"
        "---\n"
        f"<!-- {MARKER} -->\n"
        "Run this command with the Bash tool, then follow what it prints:\n\n"
        f"`\"{exe}\" -m {MARKER} map --announce`\n\n"
        "It refreshes .claude/project-map.md and .claude/project-index.md for the current project. From now on, "
        "read the map before exploring the codebase, and grep the index to find pages, routes and symbols and what "
        "depends on a file, instead of searching the tree.\n"
    )


def _install_command(path: Path, python: str, record: dict[str, Any], dry_run: bool, notes: list[str]) -> None:
    target = commands_dir(path) / f"{COMMAND_NAME}.md"
    if target.exists() and str(target) not in record.get("commands", []) and MARKER not in target.read_text(encoding="utf-8"):
        notes.append(L(f"Left untouched because you have your own /{COMMAND_NAME} command.",
                       f"Kendi /{COMMAND_NAME} komutunuz olduğu için dokunulmadı."))
        return
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(command_text(python), encoding="utf-8")
    record["commands"] = sorted(set(record.get("commands", [])) | {str(target)})
    notes.append(L(f"Command added: /{COMMAND_NAME} refreshes the map and index inside a session that is already open.",
                   f"Komut eklendi: /{COMMAND_NAME}, zaten açık bir oturumda harita ve index'i yeniler."))


# Claude Code settings the installer can set on request: (key, minimum, maximum, note(value)).
# The previous value is recorded and restored on uninstall.
SCALAR_SETTINGS = {
    "compact_window": ("autoCompactWindow", 100_000, 1_000_000, lambda v: L(f"Auto-compact threshold set to {v:,} tokens.", f"Otomatik sıkıştırma eşiği {v:,} token yapıldı.")),
    "bash_output_max": ("bashOutputMaxChars", 4_000, 128_000,
                        lambda v: L(f"Shell output over {v:,} characters will be written to a file, Claude will only see a preview.",
                                    f"Kabuk çıktısı {v:,} karakteri aşınca dosyaya yazılacak, Claude yalnızca önizleme görecek.")),
}


def install(
    path: Path,
    *,
    python: str | None = None,
    statusline: bool = False,
    compact_window: int | None = None,
    bash_output_max: int | None = None,
    agents: bool = False,
    subagent_model: str | None = None,
    model: str | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    python = python or sys.executable
    settings = _read(path)
    notes: list[str] = []
    record: dict[str, Any] = _load_records().get(str(path), {})

    _strip_ours(settings)  # reinstall cleanly
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(L(f"'hooks' in {path} is not an object, left untouched.", f"{path} içindeki 'hooks' bir nesne değil, dokunulmadı."))
    for event, groups in build_hooks(python).items():
        existing = hooks.get(event)
        hooks[event] = (existing if isinstance(existing, list) else []) + groups
    notes.append(L("Hooks added: SessionStart, UserPromptSubmit, PreToolUse (file, search, shell and subagent tools), Stop and SessionEnd (work log).",
                   "Hook'lar eklendi: SessionStart, UserPromptSubmit, PreToolUse (dosya, arama, kabuk ve alt ajan araçları), Stop ve SessionEnd (çalışma günlüğü)."))

    if statusline:
        current = settings.get("statusLine")
        ours = isinstance(current, dict) and MARKER in str(current.get("command", ""))
        if current and not ours:
            record.setdefault("previous_statusLine", current)
            notes.append(L("Existing status line was backed up and replaced. It comes back on uninstall.",
                           "Mevcut durum satırı yedeklendi ve değiştirildi. Kaldırınca geri gelir."))
        settings["statusLine"] = {"type": "command", "command": statusline_command(python)}
        notes.append(L("Status line added.", "Durum satırı eklendi."))

    requested = {"compact_window": compact_window, "bash_output_max": bash_output_max}
    for option, value in requested.items():
        if value is None:
            continue
        key, low, high, note = SCALAR_SETTINGS[option]
        if not low <= value <= high:
            raise InstallError(L(f"{key} must be between {low} and {high}.", f"{key} {low} ile {high} arasında olmalı."))
        if f"previous_{key}" not in record:
            record[f"previous_{key}"] = settings.get(key)
        settings[key] = value
        notes.append(note(value))

    _install_command(path, python, record, dry_run, notes)
    if agents:
        _install_agents(path, record, dry_run, notes)
    if model is not None:
        if not model.strip():
            raise InstallError(L("Model name cannot be empty.", "Model adı boş olamaz."))
        if "previous_model" not in record:
            record["previous_model"] = settings.get("model")
        settings["model"] = model
        notes.append(L(f"New conversations will start with {model}. In an open conversation it can be changed with /model.",
                       f"Yeni sohbetler {model} ile başlayacak. Açık bir sohbette /model ile değiştirilebilir."))
    if subagent_model is not None:
        if subagent_model not in SUBAGENT_MODELS:
            raise InstallError(L(f"Subagent model must be one of: {', '.join(SUBAGENT_MODELS)}.",
                                 f"Alt ajan modeli şunlardan biri olmalı: {', '.join(SUBAGENT_MODELS)}."))
        env = settings.setdefault("env", {})
        if not isinstance(env, dict):
            raise InstallError(L(f"'env' in {path} is not an object, left untouched.", f"{path} içindeki 'env' bir nesne değil, dokunulmadı."))
        if "previous_subagent_model" not in record:
            record["previous_subagent_model"] = env.get(SUBAGENT_ENV)
        env[SUBAGENT_ENV] = subagent_model
        notes.append(L(f"Subagents that name no model will run with {subagent_model}.",
                       f"Model belirtilmeyen alt ajanlar {subagent_model} ile çalışacak."))

    if not dry_run:
        backup = _write(path, settings)
        if backup:
            notes.append(L(f"Backup of the previous settings: {backup}", f"Önceki ayarların yedeği: {backup}"))
        records = _load_records()
        records[str(path)] = record
        _save_records(records)
    return settings, notes


def uninstall(path: Path, *, dry_run: bool = False) -> tuple[dict[str, Any], list[str]]:
    settings = _read(path)
    notes: list[str] = []
    records = _load_records()
    record = records.get(str(path), {})

    _strip_ours(settings)
    notes.append(L("Hooks removed.", "Hook'lar kaldırıldı."))
    current = settings.get("statusLine")
    if isinstance(current, dict) and MARKER in str(current.get("command", "")):
        if record.get("previous_statusLine"):
            settings["statusLine"] = record["previous_statusLine"]
            notes.append(L("Previous status line restored.", "Önceki durum satırı geri yüklendi."))
        else:
            del settings["statusLine"]
            notes.append(L("Status line removed.", "Durum satırı kaldırıldı."))
    for key, *_ in SCALAR_SETTINGS.values():
        if f"previous_{key}" not in record:
            continue
        previous = record[f"previous_{key}"]
        if previous is None:
            settings.pop(key, None)
        else:
            settings[key] = previous
        notes.append(L(f"{key} restored to its previous value.", f"{key} eski haline döndü."))
    for command in record.get("commands", []):
        command_path = Path(command)
        if command_path.exists() and not dry_run:
            command_path.unlink()
    if record.get("commands"):
        notes.append(L(f"Command /{COMMAND_NAME} removed.", f"/{COMMAND_NAME} komutu kaldırıldı."))
    for agent in record.get("agents", []):
        agent_path = Path(agent)
        if agent_path.exists() and not dry_run:
            agent_path.unlink()
    if record.get("agents"):
        notes.append(L("Subagents removed.", "Alt ajanlar kaldırıldı."))
    if "previous_model" in record:
        if record["previous_model"] is None:
            settings.pop("model", None)
        else:
            settings["model"] = record["previous_model"]
        notes.append(L("Default model restored to its previous value.", "Varsayılan model eski haline döndü."))
    if "previous_subagent_model" in record and isinstance(settings.get("env"), dict):
        previous = record["previous_subagent_model"]
        if previous is None:
            settings["env"].pop(SUBAGENT_ENV, None)
            if not settings["env"]:
                del settings["env"]
        else:
            settings["env"][SUBAGENT_ENV] = previous
        notes.append(L("Default subagent model restored to its previous value.", "Varsayılan alt ajan modeli eski haline döndü."))

    if not dry_run:
        backup = _write(path, settings) if path.exists() else None
        if backup:
            notes.append(L(f"Backup of the previous settings: {backup}", f"Önceki ayarların yedeği: {backup}"))
        records.pop(str(path), None)
        _save_records(records)
    return settings, notes
