"""Health check: are the guards installed, runnable and actually running?

Hooks fail silently by design (a broken guard must not break Claude Code), so a moved
folder or a deleted virtual environment would go unnoticed. This module checks every
link of the chain and says how to fix what is broken.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import installer
from .guard_config import DEFAULTS, home, last_heartbeats

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
SYMBOLS = {OK: "✓", INFO: "·", WARN: "!", FAIL: "✗"}
STALE_AFTER = 30 * 60  # Claude Code used this much longer ago than the last hook run: hooks are not running
REINSTALL = "Düzeltmek için: .venv/Scripts/cc-optimizer install --scope user --statusline --agents"


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "az önce"
    if seconds < 3600:
        return f"{round(seconds / 60)} dakika önce"
    if seconds < 2 * 86400:
        return f"{round(seconds / 3600)} saat önce"
    return f"{round(seconds / 86400)} gün önce"


def _our_groups(settings: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    return {
        event: [g for g in groups if installer._is_ours(g)]
        for event, groups in hooks.items()
        if isinstance(groups, list) and any(installer._is_ours(g) for g in groups)
    }


def check_settings(path: Path) -> tuple[list[Check], str | None]:
    """Hooks present and current in the settings file. Returns the python they run with."""
    try:
        settings = installer._read(path)
    except installer.InstallError as exc:
        return [Check(FAIL, "Ayar dosyası okunamıyor", f"{exc} Claude Code bu dosyadaki tüm ayarları yok sayar.")], None
    if not path.exists():
        return [Check(FAIL, "Ayar dosyası yok", f"{path}. {REINSTALL}")], None

    ours = _our_groups(settings)
    expected = installer.build_hooks("python")
    checks = [Check(OK, "Ayar dosyası", str(path))]
    missing = [e for e in expected if e not in ours]
    if missing:
        checks.append(Check(FAIL, "Eksik hook'lar", f"{', '.join(missing)}. {REINSTALL}"))
        if not ours:
            return checks, None
    outdated = [
        e for e, groups in expected.items()
        if e in ours and groups[0].get("matcher") != ours[e][0].get("matcher")
    ]
    if outdated:
        checks.append(Check(WARN, "Hook'lar eski sürümle kurulmuş", f"{', '.join(outdated)} eşleyicisi farklı. {REINSTALL}"))
    elif not missing:
        matcher = ours["PreToolUse"][0].get("matcher", "")
        checks.append(Check(OK, "Hook'lar kurulu", f"SessionStart, UserPromptSubmit, PreToolUse ({matcher})"))

    python = None
    for groups in ours.values():
        for hook in groups[0].get("hooks", []):
            if installer.MARKER in " ".join(map(str, hook.get("args") or [])):
                python = hook.get("command")
                break
        if python:
            break
    status_line = settings.get("statusLine")
    if isinstance(status_line, dict) and installer.MARKER in str(status_line.get("command", "")):
        checks.append(Check(OK, "Durum satırı kurulu"))
    else:
        checks.append(Check(INFO, "Durum satırı Service001'in değil", "İsteğe bağlı: install --statusline"))

    extras = []
    if settings.get("autoCompactWindow"):
        extras.append(f"sıkıştırma eşiği {settings['autoCompactWindow']:,}".replace(",", "."))
    if settings.get("bashOutputMaxChars"):
        extras.append(f"kabuk çıktı sınırı {settings['bashOutputMaxChars']:,}".replace(",", "."))
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    if env.get(installer.SUBAGENT_ENV):
        extras.append(f"varsayılan alt ajan modeli {env[installer.SUBAGENT_ENV]}")
    if extras:
        checks.append(Check(INFO, "Claude Code ayarları", ", ".join(extras)))
    return checks, python


def check_runtime(python: str | None) -> list[Check]:
    """Run the hook and the status line exactly as Claude Code does."""
    if not python:
        return []
    if not Path(python).is_file():
        return [Check(FAIL, "Python bulunamadı", f"{python}. Service001 klasörü veya .venv taşınmış ya da silinmiş. {REINSTALL}")]
    checks = [Check(OK, "Python", python)]
    env = {**os.environ, "CC_OPTIMIZER_NO_HEARTBEAT": "1"}
    started = time.perf_counter()
    try:
        proc = subprocess.run([python, "-m", installer.MARKER, "hook", "session-start"], input=b"{}",
                              capture_output=True, timeout=15, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return checks + [Check(FAIL, "Hook çalıştırılamadı", str(exc))]
    ms = round(1000 * (time.perf_counter() - started))
    try:
        ok = "hookSpecificOutput" in json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        ok = False
    if proc.returncode != 0 or not ok:
        error = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["çıktı yok"]
        checks.append(Check(FAIL, "Hook hata veriyor", f"{error[0]}. {REINSTALL}"))
    else:
        level = OK if ms < 3000 else WARN
        checks.append(Check(level, "Hook çalışıyor", f"{ms} ms" + ("" if level == OK else ", yavaş: her araç çağrısını geciktirir")))
    sample = json.dumps({"model": {"display_name": "Test"}, "context_window": {"context_window_size": 1000,
                         "current_usage": {"input_tokens": 1}}, "cost": {"total_cost_usd": 0}})
    try:
        out = subprocess.run([python, "-m", installer.MARKER, "statusline"], input=sample.encode(),
                             capture_output=True, timeout=15, env=env).stdout.decode("utf-8", "replace")
        checks.append(Check(OK, "Durum satırı çalışıyor") if "Test" in out else Check(WARN, "Durum satırı boş çıktı veriyor"))
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(Check(WARN, "Durum satırı çalıştırılamadı", str(exc)))
    return checks


def check_activity(projects_root: Path, now: float | None = None) -> list[Check]:
    """Did the hooks actually run while Claude Code was in use?"""
    now = now or time.time()
    beats = last_heartbeats()
    try:
        last_use = max((p.stat().st_mtime for p in projects_root.rglob("*.jsonl")), default=None)
    except OSError:
        last_use = None
    if not beats:
        if last_use and now - last_use < 3600:
            return [Check(WARN, "Koruyucuların çalıştığına dair kayıt yok",
                          "Yeni bir oturum açıp bir mesaj yazdıktan sonra tekrar kontrol edin.")]
        return [Check(INFO, "Koruyucular henüz hiç çalışmamış", "Claude Code'u kullandıktan sonra tekrar kontrol edin.")]
    last_beat = max(beats.values())
    detail = ", ".join(f"{e} {_ago(now - t)}" for e, t in sorted(beats.items(), key=lambda kv: -kv[1]))
    if last_use and last_use - last_beat > STALE_AFTER:
        return [Check(FAIL, "Koruyucular çalışmıyor",
                      f"Claude Code {_ago(now - last_use)} kullanıldı, koruyucular en son {_ago(now - last_beat)} çalıştı. "
                      f"{REINSTALL}")]
    return [Check(OK, "Koruyucular en son çalıştı", detail)]


def check_events(days: float = 7) -> list[Check]:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))
    count = 0
    try:
        for line in (home() / "events.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                count += json.loads(line).get("ts", "") >= cutoff
            except ValueError:
                continue
    except OSError:
        pass
    return [Check(INFO, f"Son {days:g} günde koruyucu olayı", f"{count}. Ayrıntı: cc-optimizer events")]


def check_config() -> list[Check]:
    path = home() / "config.json"
    if not path.exists():
        return [Check(INFO, "Koruyucu ayarları varsayılan", str(path) + " yok")]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [Check(FAIL, "config.json okunamıyor", f"{exc}. Bozuk dosya yok sayılır, varsayılanlar kullanılır.")]
    if not isinstance(data, dict):
        return [Check(FAIL, "config.json bir JSON nesnesi değil")]
    unknown = sorted(set(data) - set(DEFAULTS))
    wrong = sorted(k for k in data if k in DEFAULTS and type(data[k]) is not type(DEFAULTS[k])
                   and not (isinstance(DEFAULTS[k], (int, float)) and isinstance(data[k], (int, float))))
    checks = [Check(OK, "config.json", ", ".join(f"{k}={data[k]}" for k in sorted(data) if k in DEFAULTS) or "boş")]
    if unknown:
        checks.append(Check(WARN, "config.json içinde bilinmeyen ayarlar", f"{', '.join(unknown)}. Yazım hatası olabilir, yok sayılıyor."))
    if wrong:
        checks.append(Check(WARN, "config.json içinde türü yanlış ayarlar", ", ".join(wrong)))
    return checks


def check_agents(agents_dir: Path) -> list[Check]:
    names = sorted(p.stem for p in installer.AGENT_SOURCE.glob("*.md"))
    present = [n for n in names if (agents_dir / f"{n}.md").is_file()]
    if not present:
        return [Check(INFO, "Alt ajanlar kurulu değil", "İsteğe bağlı: install --agents")]
    outdated = [n for n in present if (agents_dir / f"{n}.md").read_text(encoding="utf-8")
                != (installer.AGENT_SOURCE / f"{n}.md").read_text(encoding="utf-8")]
    missing = sorted(set(names) - set(present))
    if missing or outdated:
        detail = "; ".join(filter(None, [missing and f"eksik: {', '.join(missing)}",
                                          outdated and f"farklı: {', '.join(outdated)}"]))
        return [Check(WARN, "Alt ajanlar", f"{detail}. Kendi değişikliğiniz değilse: install --agents")]
    return [Check(OK, "Alt ajanlar güncel", ", ".join(present))]


def check_history() -> list[Check]:
    from . import history

    if not history.db_path().exists():
        return [Check(WARN, "Token geçmişi henüz yok", "Oluşturmak için: cc-optimizer history")]
    try:
        con = history.connect()
        count = con.execute("SELECT count(*), sum(guarded = 1) FROM sessions").fetchone()
        last = history._meta(con, "last_sync")
        con.close()
    except Exception as exc:  # a broken database must not hide the other checks
        return [Check(FAIL, "Token geçmişi okunamıyor", str(exc))]
    age = time.time() - time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S")) if last else None
    detail = f"{count[0]} oturum, {count[1] or 0} tanesi Service001'li. Son güncelleme {_ago(age) if age is not None else 'yok'}"
    level = OK if age is not None and age < 3 * 86400 else WARN
    return [Check(level, "Token geçmişi", detail + ("" if level == OK else ". Arka plan güncellemesi çalışmıyor olabilir"))]


def check_project(project: Path) -> list[Check]:
    from .project_map import is_project, map_path

    checks: list[Check] = []
    for scope in ("project", "local"):
        path = installer.settings_path(scope, project)
        try:
            if _our_groups(installer._read(path)):
                checks.append(Check(WARN, "Bu projede ikinci kurulum var",
                                    f"{path}. Hook'lar iki kez çalışır. Kaldırmak için: cc-optimizer uninstall --scope {scope}"))
        except installer.InstallError:
            checks.append(Check(WARN, "Proje ayar dosyası okunamıyor", str(path)))
    if is_project(project):
        mp = map_path(project)
        if mp.is_file():
            checks.append(Check(OK, "Proje haritası", f"{mp}, {_ago(time.time() - mp.stat().st_mtime)} güncellendi"))
        else:
            checks.append(Check(INFO, "Proje haritası henüz yok", "Bu projede yeni bir oturum açınca üretilir."))
        from .worklog import log_path

        log = log_path(project)
        if log.is_file():
            entries = log.read_text(encoding="utf-8").count("<!-- session:")
            checks.append(Check(OK, "Çalışma günlüğü", f"{entries} oturum, {_ago(time.time() - log.stat().st_mtime)} güncellendi"))
        else:
            checks.append(Check(INFO, "Çalışma günlüğü henüz yok", "Bu projede bir oturum bitip yenisi açılınca yazılır."))
    return checks


def run_all(settings_file: Path, project: Path, projects_root: Path) -> list[Check]:
    checks, python = check_settings(settings_file)
    checks += check_runtime(python)
    checks += check_activity(projects_root)
    checks += check_events()
    checks += check_history()
    checks += check_config()
    checks += check_agents(installer.agents_dir(settings_file))
    checks += check_project(project)
    return checks


def render(checks: list[Check]) -> str:
    lines = ["Service001 sağlık kontrolü", ""]
    for c in checks:
        lines.append(f"{SYMBOLS[c.status]} {c.title}" + (f": {c.detail}" if c.detail else ""))
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines += ["", "Sonuç: her şey yolunda." if not fails and not warns else f"Sonuç: {fails} hata, {warns} uyarı."]
    return "\n".join(lines)
