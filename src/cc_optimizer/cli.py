"""Command line.

  analyze               where do tokens go in Claude Code sessions
  hook <event>          hook handler, called by Claude Code
  statusline            status line, called by Claude Code
  install / uninstall   add or remove the guards in a settings file
  events                what the guards did
  compare               before/after comparison with the baseline
  map                   write or refresh the project map
  doctor                health check of the installation
  history               permanent per-session token history

Imports are lazy so that hook calls start fast.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def default_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _collect(args: argparse.Namespace, since: str | None, until: str | None = None):
    from datetime import datetime, timedelta, timezone

    from .transcripts import TokenCounter, find_transcripts, parse_timeline

    counter = TokenCounter(fast=args.fast)
    project = str(Path.cwd()) if args.project == "." else args.project
    active_after = None
    if getattr(args, "days", None):
        active_after = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()

    timelines = []
    for path, proj, sid, is_sub in find_transcripts(Path(args.root), project, getattr(args, "session", None),
                                                    not getattr(args, "no_subagents", False)):
        tl = parse_timeline(path, proj, sid, is_sub, counter)
        if not tl.calls:
            continue
        if active_after and (tl.last_ts or "") < active_after:
            continue
        # since / until select sessions by start time, for before/after comparisons.
        if since and (tl.first_ts or "") < since:
            continue
        if until and (tl.first_ts or "") >= until:
            continue
        timelines.append(tl)
    return timelines, counter


def _analyze(args: argparse.Namespace) -> int:
    import time

    from .analyze import analyze
    from .report import render

    started = time.perf_counter()
    timelines, counter = _collect(args, args.since, args.until)
    rep = analyze(timelines, top=args.top, estimate_method=counter.method)
    rep["scope"]["seconds"] = round(time.perf_counter() - started, 1)
    _utf8_stdout()
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render(rep))
    return 0


def default_baseline() -> Path | None:
    data = Path(__file__).resolve().parents[2] / "data"
    found = list(data.glob("baseline-*.json"))
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _compare(args: argparse.Namespace) -> int:
    from datetime import date, timedelta

    from .analyze import analyze
    from .compare import compare, render

    if not args.baseline:
        return _compare_history(args)
    baseline = Path(args.baseline)
    if not baseline.is_file():
        print("Hata: başlangıç ölçümü bulunamadı. --baseline ile bir analyze --json çıktısı verin.", file=sys.stderr)
        return 1
    before = json.loads(baseline.read_text(encoding="utf-8"))
    since = args.since
    if not since:
        last = (before["scope"].get("last") or "")[:10]
        since = (date.fromisoformat(last) + timedelta(days=1)).isoformat() if last else None
    timelines, counter = _collect(args, since)
    if not timelines:
        print(f"{since} tarihinden sonra başlayan oturum yok. Karşılaştırma için önce veri birikmeli.")
        return 0
    after = analyze(timelines, top=5, estimate_method=counter.method)
    result = compare(before, after)
    result["since"] = since
    _utf8_stdout()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        labelled = {EVENT_LABELS.get(k, k): n for k, n in _event_counts(since).items()}
        print(render(result, labelled))
    return 0


def _compare_history(args: argparse.Namespace) -> int:
    from .compare import compare, render
    from .history import as_report, connect, rows, sync

    con = connect()
    sync(Path(args.root), con, fast=args.fast)
    before, after = rows(con, args.project, guarded=False), rows(con, args.project, guarded=True)
    scope = f" ({args.project})" if args.project else ""
    if not before or not after:
        print(f"Karşılaştırma için iki taraf da gerekli{scope}: Service001'siz {len(before)}, Service001'li {len(after)} oturum.")
        return 0
    result = compare(as_report(before), as_report(after))
    _utf8_stdout()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    since = min(r["first_ts"] or "" for r in after)
    labelled = {EVENT_LABELS.get(k, k): n for k, n in _event_counts(since).items()}
    print(render(result, labelled, labels=("Service001'siz", "Service001'li"), title_suffix=scope))
    return 0


def _history(args: argparse.Namespace) -> int:
    from .history import connect, render_history, sync

    con = connect()
    _utf8_stdout()
    if not args.no_sync:
        stats = sync(Path(args.root), con, fast=args.fast)
        if args.sync_only:
            return 0
        if stats["updated"]:
            print(f"{stats['updated']} oturum kayda eklendi veya güncellendi.")
    _utf8_stdout()
    print(render_history(con, args.project, args.weeks))
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from .doctor import FAIL, render, run_all
    from .installer import settings_path

    checks = run_all(settings_path("user"), Path(args.project_dir or Path.cwd()), default_root())
    _utf8_stdout()
    print(render(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


def _map(args: argparse.Namespace) -> int:
    from .project_map import build_map, ensure_map

    project = Path(args.project_dir or Path.cwd())
    _utf8_stdout()
    if args.print:
        print(build_map(project))
        return 0
    path = ensure_map(project, force=True)
    if not path:
        print(f"{project} bir proje klasörü gibi görünmüyor (git, package.json, pyproject.toml vb. yok).")
        return 1
    print(f"Proje haritası yazıldı: {path}")
    return 0


def _hook(args: argparse.Namespace) -> int:
    from .hooks import run

    output = run(args.event, sys.stdin.read())
    if output:
        _utf8_stdout()
        print(output)
    return 0


def _statusline(args: argparse.Namespace) -> int:
    from .statusline import run

    _utf8_stdout()
    print(run(sys.stdin.read()))
    return 0


def _install(args: argparse.Namespace, remove: bool = False) -> int:
    from .installer import InstallError, install, settings_path, uninstall

    path = settings_path(args.scope, args.project_dir)
    try:
        if remove:
            settings, notes = uninstall(path, dry_run=args.dry_run)
        else:
            settings, notes = install(
                path, statusline=args.statusline, compact_window=args.compact_window,
                bash_output_max=args.bash_output_max, agents=args.agents,
                subagent_model=args.subagent_model, model=args.model, dry_run=args.dry_run,
            )
    except InstallError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1
    _utf8_stdout()
    print(f"Ayar dosyası: {path}")
    for note in notes:
        print(f"- {note}")
    if args.dry_run:
        print("Deneme modu: dosya değiştirilmedi. Yazılacak içerik:")
        print(json.dumps(settings, indent=2, ensure_ascii=False))
    else:
        print("Yeni oturumlarda geçerli olur. Açık bir oturumda /hooks menüsünü bir kez açmak ayarları yeniden yükler.")
    return 0


EVENT_LABELS = {
    "context_warning": "Bağlam uzunluğu uyarısı",
    "write_rewrite": "Write ile baştan yazma uyarısı",
    "write_rewrite_blocked": "Write ile baştan yazma engeli",
    "write_rewrite_retried": "Engelden sonra izin verilen tekrar",
    "heredoc_rewrite": "Heredoc ile baştan yazma uyarısı",
    "read_large_blocked": "Büyük dosyanın tamamını okuma engeli",
    "read_large_retried": "Engelden sonra izin verilen tam okuma",
    "read_large": "Büyük dosyanın tamamını okuma uyarısı",
    "shell_read_blocked": "Kabukta büyük dosya basma engeli",
    "shell_read_retried": "Engelden sonra izin verilen dosya basma",
    "shell_read": "Kabukta büyük dosya basma uyarısı",
    "model_advice": "İlk promptta model önerisi istendi",
    "routing_nudge": "Keşfi scout'a (Haiku) devretme hatırlatması",
    "subagent_call": "Alt ajan çağrısı",
}


def _event_counts(since: str) -> dict[str, int]:
    from collections import Counter

    from .guard_config import home

    kinds: Counter = Counter()
    try:
        lines = (home() / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("ts", "") >= since:
            kinds[event.get("kind", "?")] += 1
    return dict(kinds)


def _events(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S")
    kinds = _event_counts(cutoff)
    _utf8_stdout()
    print(f"Son {args.days:g} günde koruyucu olayları:")
    if not kinds:
        print("- kayıt yok")
    for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"- {EVENT_LABELS.get(kind, kind)}: {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cc-optimizer", description="Claude Code token analysis and guards")
    sub = parser.add_subparsers(dest="command", required=True)

    an = sub.add_parser("analyze", help="report where tokens go in Claude Code sessions")
    an.add_argument("--root", default=str(default_root()), help="transcript folder (default: ~/.claude/projects)")
    an.add_argument("--project", default=None, help="project path, or '.' for the current folder (default: all)")
    an.add_argument("--session", default=None, help="session id or prefix")
    an.add_argument("--days", type=float, default=None, help="only sessions active in the last N days")
    an.add_argument("--since", default=None, help="only sessions started at or after this date (YYYY-MM-DD)")
    an.add_argument("--until", default=None, help="only sessions started before this date (YYYY-MM-DD)")
    an.add_argument("--top", type=int, default=15)
    an.add_argument("--no-subagents", action="store_true")
    an.add_argument("--fast", action="store_true", help="estimate with chars/4 instead of tiktoken")
    an.add_argument("--json", action="store_true")
    an.set_defaults(func=_analyze)

    hk = sub.add_parser("hook", help="hook handler (called by Claude Code)")
    hk.add_argument("event", choices=["session-start", "user-prompt-submit", "pre-tool-use"])
    hk.set_defaults(func=_hook)

    sl = sub.add_parser("statusline", help="status line (called by Claude Code)")
    sl.set_defaults(func=_statusline)

    for name, remove in (("install", False), ("uninstall", True)):
        p = sub.add_parser(name, help=f"{name} the guards in a Claude Code settings file")
        p.add_argument("--scope", choices=["user", "project", "local"], default="user",
                       help="user: ~/.claude/settings.json, project: .claude/settings.json, local: .claude/settings.local.json")
        p.add_argument("--project-dir", default=None, help="project folder for project/local scope (default: current)")
        p.add_argument("--dry-run", action="store_true", help="show the result without writing")
        if not remove:
            p.add_argument("--statusline", action="store_true", help="also install the status line")
            p.add_argument("--compact-window", type=int, default=None,
                           help="set Claude Code's autoCompactWindow, e.g. 400000")
            p.add_argument("--bash-output-max", type=int, default=None,
                           help="set Claude Code's bashOutputMaxChars (4000-128000): longer output goes to a file")
            p.add_argument("--agents", action="store_true",
                           help="install the scout (Haiku), worker (Sonnet) and expert (Fable) subagents")
            p.add_argument("--subagent-model", choices=["haiku", "sonnet", "opus", "fable", "inherit"], default=None,
                           help="default model for subagents that do not name one (CLAUDE_CODE_SUBAGENT_MODEL)")
            p.add_argument("--model", default=None,
                           help="model new conversations start with, e.g. sonnet (the previous one is restored on uninstall)")
        p.set_defaults(func=lambda a, r=remove: _install(a, remove=r))

    hs = sub.add_parser("history", help="permanent per-session token history")
    hs.add_argument("--project", default=None, help="project name or part of it, e.g. KEIMORA")
    hs.add_argument("--weeks", type=int, default=8)
    hs.add_argument("--root", default=str(default_root()))
    hs.add_argument("--fast", action="store_true")
    hs.add_argument("--sync-only", action="store_true", help="only record new sessions (used in the background)")
    hs.add_argument("--no-sync", action="store_true", help="show the history without reading transcripts")
    hs.set_defaults(func=_history)

    dr = sub.add_parser("doctor", help="check that the guards are installed, runnable and running")
    dr.add_argument("--project-dir", default=None, help="project folder to check as well (default: current)")
    dr.set_defaults(func=_doctor)

    mp = sub.add_parser("map", help="write or refresh the project map (.claude/project-map.md)")
    mp.add_argument("--project-dir", default=None, help="project folder (default: current)")
    mp.add_argument("--print", action="store_true", help="print the map instead of writing it")
    mp.set_defaults(func=_map)

    cp = sub.add_parser("compare", help="compare sessions after installing the guards with the baseline")
    cp.add_argument("--baseline", default=None,
                    help="compare transcripts with this analyze --json file instead of using the history")
    cp.add_argument("--since", default=None, help="with --baseline: sessions started at or after this date")
    cp.add_argument("--root", default=str(default_root()))
    cp.add_argument("--project", default=None, help="project name or part of it, e.g. KEIMORA")
    cp.add_argument("--fast", action="store_true")
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=_compare)

    ev = sub.add_parser("events", help="what the guards did")
    ev.add_argument("--days", type=float, default=7.0)
    ev.set_defaults(func=_events)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
