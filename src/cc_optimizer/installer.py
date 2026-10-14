"""Add or remove the context guards in a Claude Code settings file.

The settings file is merged, never replaced: existing hooks and keys are kept,
a timestamped backup is written first, and values we override (statusLine,
autoCompactWindow) are recorded so that uninstall can restore them.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .guard_config import home

MARKER = "cc_optimizer"
SCOPES = ("user", "project", "local")


class InstallError(Exception):
    pass


def settings_path(scope: str, project_dir: str | Path | None = None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    base = Path(project_dir or Path.cwd())
    return base / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")


def _hook(python: str, event: str) -> dict[str, Any]:
    # Exec form: no shell, so paths with spaces or backslashes need no quoting.
    return {"type": "command", "command": python, "args": ["-m", MARKER, "hook", event], "timeout": 10}


def build_hooks(python: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "SessionStart": [{"matcher": "startup|clear|compact", "hooks": [_hook(python, "session-start")]}],
        "UserPromptSubmit": [{"hooks": [_hook(python, "user-prompt-submit")]}],
        "PreToolUse": [{"matcher": "Write|Edit|MultiEdit|NotebookEdit|Read|Grep|Glob|Bash|PowerShell|Agent|Task", "hooks": [_hook(python, "pre-tool-use")]}],
    }


def statusline_command(python: str) -> str:
    # statusLine has no exec form and runs through a shell.
    return f'"{Path(python).as_posix()}" -m {MARKER} statusline'


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
        raise InstallError(f"{path} geçerli JSON değil, dokunulmadı: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} bir JSON nesnesi değil, dokunulmadı.")
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
        notes.append(f"Alt ajanlar kuruldu ({target_dir}): {', '.join(written)}.")
    if skipped:
        notes.append(f"Aynı adda kendi ajanınız olduğu için dokunulmadı: {', '.join(skipped)}.")


# Claude Code settings the installer can set on request: (key, minimum, maximum, note).
# The previous value is recorded and restored on uninstall.
SCALAR_SETTINGS = {
    "compact_window": ("autoCompactWindow", 100_000, 1_000_000, "Otomatik sıkıştırma eşiği {:,} token yapıldı."),
    "bash_output_max": ("bashOutputMaxChars", 4_000, 128_000,
                        "Kabuk çıktısı {:,} karakteri aşınca dosyaya yazılacak, Claude yalnızca önizleme görecek."),
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
        raise InstallError(f"{path} içindeki 'hooks' bir nesne değil, dokunulmadı.")
    for event, groups in build_hooks(python).items():
        existing = hooks.get(event)
        hooks[event] = (existing if isinstance(existing, list) else []) + groups
    notes.append("Hook'lar eklendi: SessionStart, UserPromptSubmit, PreToolUse (dosya, arama, kabuk ve alt ajan araçları).")

    if statusline:
        current = settings.get("statusLine")
        ours = isinstance(current, dict) and MARKER in str(current.get("command", ""))
        if current and not ours:
            record.setdefault("previous_statusLine", current)
            notes.append("Mevcut durum satırı yedeklendi ve değiştirildi. Kaldırınca geri gelir.")
        settings["statusLine"] = {"type": "command", "command": statusline_command(python)}
        notes.append("Durum satırı eklendi.")

    requested = {"compact_window": compact_window, "bash_output_max": bash_output_max}
    for option, value in requested.items():
        if value is None:
            continue
        key, low, high, note = SCALAR_SETTINGS[option]
        if not low <= value <= high:
            raise InstallError(f"{key} {low} ile {high} arasında olmalı.")
        if f"previous_{key}" not in record:
            record[f"previous_{key}"] = settings.get(key)
        settings[key] = value
        notes.append(note.format(value))

    if agents:
        _install_agents(path, record, dry_run, notes)
    if model is not None:
        if not model.strip():
            raise InstallError("Model adı boş olamaz.")
        if "previous_model" not in record:
            record["previous_model"] = settings.get("model")
        settings["model"] = model
        notes.append(f"Yeni sohbetler {model} ile başlayacak. Açık bir sohbette /model ile değiştirilebilir.")
    if subagent_model is not None:
        if subagent_model not in SUBAGENT_MODELS:
            raise InstallError(f"Alt ajan modeli şunlardan biri olmalı: {', '.join(SUBAGENT_MODELS)}.")
        env = settings.setdefault("env", {})
        if not isinstance(env, dict):
            raise InstallError(f"{path} içindeki 'env' bir nesne değil, dokunulmadı.")
        if "previous_subagent_model" not in record:
            record["previous_subagent_model"] = env.get(SUBAGENT_ENV)
        env[SUBAGENT_ENV] = subagent_model
        notes.append(f"Model belirtilmeyen alt ajanlar {subagent_model} ile çalışacak.")

    if not dry_run:
        backup = _write(path, settings)
        if backup:
            notes.append(f"Önceki ayarların yedeği: {backup}")
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
    notes.append("Hook'lar kaldırıldı.")
    current = settings.get("statusLine")
    if isinstance(current, dict) and MARKER in str(current.get("command", "")):
        if record.get("previous_statusLine"):
            settings["statusLine"] = record["previous_statusLine"]
            notes.append("Önceki durum satırı geri yüklendi.")
        else:
            del settings["statusLine"]
            notes.append("Durum satırı kaldırıldı.")
    for key, *_ in SCALAR_SETTINGS.values():
        if f"previous_{key}" not in record:
            continue
        previous = record[f"previous_{key}"]
        if previous is None:
            settings.pop(key, None)
        else:
            settings[key] = previous
        notes.append(f"{key} eski haline döndü.")
    for agent in record.get("agents", []):
        agent_path = Path(agent)
        if agent_path.exists() and not dry_run:
            agent_path.unlink()
    if record.get("agents"):
        notes.append("Alt ajanlar kaldırıldı.")
    if "previous_model" in record:
        if record["previous_model"] is None:
            settings.pop("model", None)
        else:
            settings["model"] = record["previous_model"]
        notes.append("Varsayılan model eski haline döndü.")
    if "previous_subagent_model" in record and isinstance(settings.get("env"), dict):
        previous = record["previous_subagent_model"]
        if previous is None:
            settings["env"].pop(SUBAGENT_ENV, None)
            if not settings["env"]:
                del settings["env"]
        else:
            settings["env"][SUBAGENT_ENV] = previous
        notes.append("Varsayılan alt ajan modeli eski haline döndü.")

    if not dry_run:
        backup = _write(path, settings) if path.exists() else None
        if backup:
            notes.append(f"Önceki ayarların yedeği: {backup}")
        records.pop(str(path), None)
        _save_records(records)
    return settings, notes
