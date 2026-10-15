"""Health check: are the guards installed, runnable and actually running?

Hooks fail silently by design (a broken guard must not break Claude Code), so a moved
folder or a deleted virtual environment would go unnoticed. This module checks every
link of the chain and says how to fix what is broken.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import installer
from .guard_config import DEFAULTS, home, last_heartbeats
from .i18n import L, decimal, plural

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
SYMBOLS = {OK: "✓", INFO: "·", WARN: "!", FAIL: "✗"}
STALE_AFTER = 30 * 60  # Claude Code used this much longer ago than the last hook run: hooks are not running


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""


def _reinstall() -> str:
    return L("To fix: .venv/Scripts/cc-optimizer install --scope user --statusline --agents",
             "Düzeltmek için: .venv/Scripts/cc-optimizer install --scope user --statusline --agents")


def _ago(seconds: float) -> str:
    if seconds < 90:
        return L("just now", "az önce")
    if seconds < 3600:
        n = round(seconds / 60)
        return L(f"{n} minutes ago", f"{n} dakika önce")
    if seconds < 2 * 86400:
        n = round(seconds / 3600)
        return L(f"{n} hours ago", f"{n} saat önce")
    n = round(seconds / 86400)
    return L(f"{n} days ago", f"{n} gün önce")


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
        return [Check(FAIL, L("Settings file unreadable", "Ayar dosyası okunamıyor"),
                       L(f"{exc} Claude Code ignores every setting in this file.", f"{exc} Claude Code bu dosyadaki tüm ayarları yok sayar."))], None
    if not path.exists():
        return [Check(FAIL, L("Settings file missing", "Ayar dosyası yok"), f"{path}. {_reinstall()}")], None

    ours = _our_groups(settings)
    expected = installer.build_hooks("python")
    checks = [Check(OK, L("Settings file", "Ayar dosyası"), str(path))]
    missing = [e for e in expected if e not in ours]
    if missing:
        checks.append(Check(FAIL, L("Missing hooks", "Eksik hook'lar"), f"{', '.join(missing)}. {_reinstall()}"))
        if not ours:
            return checks, None
    outdated = [
        e for e, groups in expected.items()
        if e in ours and groups[0].get("matcher") != ours[e][0].get("matcher")
    ]
    if outdated:
        checks.append(Check(WARN, L("Hooks installed by an older version", "Hook'lar eski sürümle kurulmuş"),
                      L(f"{', '.join(outdated)} matcher differs. {_reinstall()}", f"{', '.join(outdated)} eşleyicisi farklı. {_reinstall()}")))
    elif not missing:
        matcher = ours["PreToolUse"][0].get("matcher", "")
        checks.append(Check(OK, L("Hooks installed", "Hook'lar kurulu"), f"SessionStart, UserPromptSubmit, PreToolUse ({matcher}), Stop, SessionEnd"))

    python = None
    for groups in ours.values():
        for hook in groups[0].get("hooks", []):
            if installer.MARKER in " ".join(map(str, hook.get("args") or [])):
                python = hook.get("command") and installer.console(hook["command"])  # probes want stderr
                break
        if python:
            break
    status_line = settings.get("statusLine")
    if isinstance(status_line, dict) and installer.MARKER in str(status_line.get("command", "")):
        checks.append(Check(OK, L("Status line installed", "Durum satırı kurulu")))
    else:
        checks.append(Check(INFO, L("Status line is not Service001's", "Durum satırı Service001'in değil"),
                            L("Optional: install --statusline", "İsteğe bağlı: install --statusline")))

    extras = []
    if settings.get("autoCompactWindow"):
        value = decimal(f"{settings['autoCompactWindow']:,}")
        extras.append(L(f"compaction threshold {value}", f"sıkıştırma eşiği {value}"))
    if settings.get("bashOutputMaxChars"):
        value = decimal(f"{settings['bashOutputMaxChars']:,}")
        extras.append(L(f"shell output limit {value}", f"kabuk çıktı sınırı {value}"))
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    if env.get(installer.SUBAGENT_ENV):
        model = env[installer.SUBAGENT_ENV]
        extras.append(L(f"default subagent model {model}", f"varsayılan alt ajan modeli {model}"))
    if extras:
        checks.append(Check(INFO, L("Claude Code settings", "Claude Code ayarları"), ", ".join(extras)))
    return checks, python


HOOK_EVENTS = ("user-prompt-submit", "pre-tool-use", "stop", "session-end")  # session-start is checked on its own


def _failing_events(python: str, env: dict[str, str]) -> list[str]:
    """Run every other hook event with an empty payload, as Claude Code would. A usage error or a crash is a failure."""
    failing = []
    for event in HOOK_EVENTS:
        try:
            proc = subprocess.run([python, "-m", installer.MARKER, "hook", event], input=b"{}",
                                  capture_output=True, timeout=15, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            failing.append(f"{event}: {exc}")
            continue
        if proc.returncode != 0:
            last = (proc.stderr.decode("utf-8", "replace").strip().splitlines() or [L("no output", "çıktı yok")])[-1]
            failing.append(f"{event}: {last}")
    return failing


def check_runtime(python: str | None) -> list[Check]:
    """Run the hook and the status line exactly as Claude Code does."""
    if not python:
        return []
    if not Path(python).is_file():
        return [Check(FAIL, L("Python not found", "Python bulunamadı"),
                       L(f"{python}. The Service001 folder or .venv was moved or deleted. {_reinstall()}",
                         f"{python}. Service001 klasörü veya .venv taşınmış ya da silinmiş. {_reinstall()}"))]
    checks = [Check(OK, "Python", python)]
    env = {**installer.clean_env(), "CC_OPTIMIZER_NO_HEARTBEAT": "1"}  # no PYTHONPATH: a false "works" helps nobody
    started = time.perf_counter()
    try:
        proc = subprocess.run([python, "-m", installer.MARKER, "hook", "session-start"], input=b"{}",
                              capture_output=True, timeout=15, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return checks + [Check(FAIL, L("Hook could not be run", "Hook çalıştırılamadı"), str(exc))]
    ms = round(1000 * (time.perf_counter() - started))
    try:
        ok = "hookSpecificOutput" in json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        ok = False
    if proc.returncode != 0 or not ok:
        error = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [L("no output", "çıktı yok")]
        checks.append(Check(FAIL, L("Hook is failing", "Hook hata veriyor"), f"{error[0]}. {_reinstall()}"))
    else:
        level = OK if ms < 3000 else WARN
        checks.append(Check(level, L("Hook works", "Hook çalışıyor"),
                           f"{ms} ms" + ("" if level == OK else L(", slow: it delays every tool call", ", yavaş: her araç çağrısını geciktirir"))))
    failing = _failing_events(python, env)
    if failing:
        checks.append(Check(FAIL, L("Some hook events are failing", "Bazı hook olayları hata veriyor"),
                            f"{'; '.join(failing)}. {_reinstall()}"))
    else:
        checks.append(Check(OK, L("All hook events run", "Tüm hook olayları çalışıyor")))
    sample = json.dumps({"model": {"display_name": "Test"}, "context_window": {"context_window_size": 1000,
                         "current_usage": {"input_tokens": 1}}, "cost": {"total_cost_usd": 0}})
    try:
        out = subprocess.run([python, "-m", installer.MARKER, "statusline"], input=sample.encode(),
                             capture_output=True, timeout=15, env=env).stdout.decode("utf-8", "replace")
        checks.append(Check(OK, L("Status line works", "Durum satırı çalışıyor")) if "Test" in out \
                       else Check(WARN, L("Status line prints nothing", "Durum satırı boş çıktı veriyor")))
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(Check(WARN, L("Status line could not be run", "Durum satırı çalıştırılamadı"), str(exc)))
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
            return [Check(WARN, L("No record of the guards running", "Koruyucuların çalıştığına dair kayıt yok"),
                          L("Open a new session, send a message, then check again.",
                            "Yeni bir oturum açıp bir mesaj yazdıktan sonra tekrar kontrol edin."))]
        return [Check(INFO, L("The guards have not run yet", "Koruyucular henüz hiç çalışmamış"),
                      L("Check again after using Claude Code.", "Claude Code'u kullandıktan sonra tekrar kontrol edin."))]
    last_beat = max(beats.values())
    detail = ", ".join(f"{e} {_ago(now - t)}" for e, t in sorted(beats.items(), key=lambda kv: -kv[1]))
    if last_use and last_use - last_beat > STALE_AFTER:
        return [Check(FAIL, L("The guards are not running", "Koruyucular çalışmıyor"),
                      L(f"Claude Code was used {_ago(now - last_use)}, the guards last ran {_ago(now - last_beat)}. ",
                        f"Claude Code {_ago(now - last_use)} kullanıldı, koruyucular en son {_ago(now - last_beat)} çalıştı. ")
                      + _reinstall())]
    return [Check(OK, L("The guards last ran", "Koruyucular en son çalıştı"), detail)]


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
    return [Check(INFO, L(f"Guard events in the last {days:g} days", f"Son {days:g} günde koruyucu olayı"),
                  L(f"{count}. Details: cc-optimizer events", f"{count}. Ayrıntı: cc-optimizer events"))]


def check_config() -> list[Check]:
    path = home() / "config.json"
    if not path.exists():
        return [Check(INFO, L("Guard settings are defaults", "Koruyucu ayarları varsayılan"),
                       str(path) + L(" is missing", " yok"))]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [Check(FAIL, L("config.json unreadable", "config.json okunamıyor"),
                       L(f"{exc}. The broken file is ignored and the defaults are used.",
                         f"{exc}. Bozuk dosya yok sayılır, varsayılanlar kullanılır."))]
    if not isinstance(data, dict):
        return [Check(FAIL, L("config.json is not a JSON object", "config.json bir JSON nesnesi değil"))]
    unknown = sorted(set(data) - set(DEFAULTS))
    wrong = sorted(k for k in data if k in DEFAULTS and type(data[k]) is not type(DEFAULTS[k])
                   and not (isinstance(DEFAULTS[k], (int, float)) and isinstance(data[k], (int, float))))
    checks = [Check(OK, "config.json", ", ".join(f"{k}={data[k]}" for k in sorted(data) if k in DEFAULTS) or L("empty", "boş"))]
    if unknown:
        checks.append(Check(WARN, L("Unknown settings in config.json", "config.json içinde bilinmeyen ayarlar"),
                      L(f"{', '.join(unknown)}. Possibly a typo, ignored.", f"{', '.join(unknown)}. Yazım hatası olabilir, yok sayılıyor.")))
    if wrong:
        checks.append(Check(WARN, L("Settings with the wrong type in config.json", "config.json içinde türü yanlış ayarlar"), ", ".join(wrong)))
    return checks


def check_agents(agents_dir: Path) -> list[Check]:
    names = sorted(p.stem for p in installer.AGENT_SOURCE.glob("*.md"))
    present = [n for n in names if (agents_dir / f"{n}.md").is_file()]
    if not present:
        return [Check(INFO, L("Subagents are not installed", "Alt ajanlar kurulu değil"),
                       L("Optional: install --agents", "İsteğe bağlı: install --agents"))]
    outdated = [n for n in present if (agents_dir / f"{n}.md").read_text(encoding="utf-8")
                != (installer.AGENT_SOURCE / f"{n}.md").read_text(encoding="utf-8")]
    missing = sorted(set(names) - set(present))
    if missing or outdated:
        detail = "; ".join(filter(None, [
            missing and L(f"missing: {', '.join(missing)}", f"eksik: {', '.join(missing)}"),
            outdated and L(f"different: {', '.join(outdated)}", f"farklı: {', '.join(outdated)}")]))
        return [Check(WARN, L("Subagents", "Alt ajanlar"),
                       L(f"{detail}. If these are not your own changes: install --agents",
                         f"{detail}. Kendi değişikliğiniz değilse: install --agents"))]
    return [Check(OK, L("Subagents up to date", "Alt ajanlar güncel"), ", ".join(present))]


def check_model(settings_file: Path, recent: int = 3) -> list[Check]:
    """Does the configured default model match what the latest sessions actually ran on?"""
    from . import history

    try:
        wanted = json.loads(settings_file.read_text(encoding="utf-8")).get("model")
    except (OSError, ValueError):
        return []
    if not isinstance(wanted, str) or not history.db_path().exists():
        return []
    try:
        con = history.connect()
        found = con.execute("SELECT models_main FROM sessions ORDER BY first_ts DESC LIMIT ?", (recent,)).fetchall()
        con.close()
    except Exception:
        return []
    ran = []
    for (raw,) in found:
        counts = json.loads(raw or "{}")
        if counts:
            ran.append(max(counts, key=counts.get))
    if not ran:
        return []
    family = wanted.lower().split("[")[0].replace("claude-", "")
    family = next((f for f in ("opus", "sonnet", "haiku", "fable") if f in family), family)
    off = [m for m in ran if family not in m]
    if len(off) * 2 <= len(ran):
        return [Check(OK, L("Default model", "Varsayılan model"),
                      L(f"{wanted}, the latest sessions match it", f"{wanted}, son oturumlar da buna uygun"))]
    return [Check(WARN, L("Default model is not applied", "Varsayılan model uygulanmıyor"),
                  L(f"The setting says {wanted}, but {len(off)} of the last {len(ran)} sessions ran on {off[0]}. "
                    "Look at where the model comes from when a chat opens (the header shows it). A project setting, "
                    "the ANTHROPIC_MODEL environment variable or another model saved in the picker may override it",
                    f"Ayarda model {wanted}, ama son {len(ran)} oturumun {len(off)} tanesi {off[0]} ile çalıştı. "
                    "Sohbet açılırken modelin hangi dosyadan geldiğine bakın (başlıkta yazar). "
                    "Proje ayarı, ANTHROPIC_MODEL ortam değişkeni veya seçicide kaydedilmiş başka bir model ayarı "
                    "geçersiz kılıyor olabilir"))]


def check_history() -> list[Check]:
    from . import history

    if not history.db_path().exists():
        return [Check(WARN, L("No token history yet", "Token geçmişi henüz yok"),
                       L("To create it: cc-optimizer history", "Oluşturmak için: cc-optimizer history"))]
    try:
        con = history.connect()
        count = con.execute("SELECT count(*), sum(guarded = 1) FROM sessions").fetchone()
        last = history._meta(con, "last_sync")
        con.close()
    except Exception as exc:  # a broken database must not hide the other checks
        return [Check(FAIL, L("Token history unreadable", "Token geçmişi okunamıyor"), str(exc))]
    age = time.time() - time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S")) if last else None
    updated = _ago(age) if age is not None else L("never", "yok")
    detail = L(f"{plural(count[0], 'session')}, {count[1] or 0} of them with Service001. Last update: {updated}",
               f"{count[0]} oturum, {count[1] or 0} tanesi Service001'li. Son güncelleme {updated}")
    level = OK if age is not None and age < 3 * 86400 else WARN
    return [Check(level, L("Token history", "Token geçmişi"),
                   detail + ("" if level == OK else L(". The background update may not be running", ". Arka plan güncellemesi çalışmıyor olabilir")))]


def check_project(project: Path) -> list[Check]:
    from .project_map import is_project, map_path

    checks: list[Check] = []
    for scope in ("project", "local"):
        path = installer.settings_path(scope, project)
        try:
            if _our_groups(installer._read(path)):
                checks.append(Check(WARN, L("This project has a second installation", "Bu projede ikinci kurulum var"),
                                    L(f"{path}. The hooks run twice. To remove it: cc-optimizer uninstall --scope {scope}",
                                      f"{path}. Hook'lar iki kez çalışır. Kaldırmak için: cc-optimizer uninstall --scope {scope}")))
        except installer.InstallError:
            checks.append(Check(WARN, L("Project settings file unreadable", "Proje ayar dosyası okunamıyor"), str(path)))
    if is_project(project):
        mp = map_path(project)
        if mp.is_file():
            checks.append(Check(OK, L("Project map", "Proje haritası"),
                                 L(f"{mp}, updated {_ago(time.time() - mp.stat().st_mtime)}",
                                   f"{mp}, {_ago(time.time() - mp.stat().st_mtime)} güncellendi")))
        else:
            checks.append(Check(INFO, L("No project map yet", "Proje haritası henüz yok"),
                                 L("Created when a new session starts in this project.", "Bu projede yeni bir oturum açınca üretilir.")))
        from .worklog import log_path

        log = log_path(project)
        if log.is_file():
            entries = log.read_text(encoding="utf-8").count("<!-- session:")
            checks.append(Check(OK, L("Work log", "Çalışma günlüğü"),
                                 L(f"{plural(entries, 'session')}, updated {_ago(time.time() - log.stat().st_mtime)}",
                                   f"{entries} oturum, {_ago(time.time() - log.stat().st_mtime)} güncellendi")))
        else:
            checks.append(Check(INFO, L("No work log yet", "Çalışma günlüğü henüz yok"),
                                 L("Written when a session ends and a new one starts in this project.", "Bu projede bir oturum bitip yenisi açılınca yazılır.")))
    return checks


def run_all(settings_file: Path, project: Path, projects_root: Path) -> list[Check]:
    checks, python = check_settings(settings_file)
    checks += check_runtime(python)
    checks += check_activity(projects_root)
    checks += check_events()
    checks += check_history()
    checks += check_model(settings_file)
    checks += check_config()
    checks += check_agents(installer.agents_dir(settings_file))
    checks += check_project(project)
    return checks


def render(checks: list[Check]) -> str:
    lines = [L("Service001 health check", "Service001 sağlık kontrolü"), ""]
    for c in checks:
        lines.append(f"{SYMBOLS[c.status]} {c.title}" + (f": {c.detail}" if c.detail else ""))
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    if not fails and not warns:
        summary = L("Result: all good.", "Sonuç: her şey yolunda.")
    else:
        summary = L(f"Result: {plural(fails, 'error')}, {plural(warns, 'warning')}.", f"Sonuç: {fails} hata, {warns} uyarı.")
    lines += ["", summary]
    return "\n".join(lines)
