"""Command line.

  analyze               where do tokens go in Claude Code sessions
  hook <event>          hook handler, called by Claude Code
  statusline            status line, called by Claude Code
  install / uninstall   add or remove the guards in a settings file
  events                what the guards did
  compare               before/after comparison with the baseline
  map                   write or refresh the project map
  impact <file>         which files are affected when a file changes
  doctor                health check of the installation
  history               permanent per-session token history

Imports are lazy so that hook calls start fast.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .i18n import L, plural


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
        print(L("Error: baseline not found. Pass an analyze --json output with --baseline.",
                "Hata: başlangıç ölçümü bulunamadı. --baseline ile bir analyze --json çıktısı verin."), file=sys.stderr)
        return 1
    before = json.loads(baseline.read_text(encoding="utf-8"))
    since = args.since
    if not since:
        last = (before["scope"].get("last") or "")[:10]
        since = (date.fromisoformat(last) + timedelta(days=1)).isoformat() if last else None
    timelines, counter = _collect(args, since)
    if not timelines:
        print(L(f"No sessions started after {since}. Data has to accumulate before a comparison is possible.",
                f"{since} tarihinden sonra başlayan oturum yok. Karşılaştırma için önce veri birikmeli."))
        return 0
    after = analyze(timelines, top=5, estimate_method=counter.method)
    result = compare(before, after)
    result["since"] = since
    _utf8_stdout()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        labels = _event_labels()
        labelled = {labels.get(k, k): n for k, n in _event_counts(since).items()}
        print(render(result, labelled))
    return 0


def _compare_history(args: argparse.Namespace) -> int:
    from .compare import compare, render, render_markdown
    from .history import as_report, connect, project_mix, project_overlap, rows, session_samples, sync

    con = connect()
    sync(Path(args.root), con, fast=args.fast)
    before, after = rows(con, args.project, guarded=False), rows(con, args.project, guarded=True)
    scope = f" ({args.project})" if args.project else ""
    if not before or not after:
        print(L(f"Both sides are needed for a comparison{scope}: {plural(len(before), 'session')} without Service001, {len(after)} with Service001.",
                f"Karşılaştırma için iki taraf da gerekli{scope}: Service001'siz {len(before)}, Service001'li {len(after)} oturum."))
        return 0
    mix = {"before": project_mix(before), "after": project_mix(after)}
    if args.matched:
        common = {r["project"] for r in before} & {r["project"] for r in after}
        if not common:
            print(L(f"No project has sessions on both sides, so there is nothing to match: without Service001 "
                    f"{', '.join(sorted(mix['before'])[:3])}; with Service001 {', '.join(sorted(mix['after'])[:3])}.",
                    f"İki tarafta da oturumu olan proje yok, eşleştirilecek bir şey yok: Service001'siz "
                    f"{', '.join(sorted(mix['before'])[:3])}; Service001'li {', '.join(sorted(mix['after'])[:3])}."))
            return 0
        before, after = [r for r in before if r["project"] in common], [r for r in after if r["project"] in common]
        mix = {"before": project_mix(before), "after": project_mix(after)}
        scope += L(f" (matched: {plural(len(common), 'project')})", f" (eşleşen: {len(common)} proje)")
    result = compare(as_report(before), as_report(after), session_samples(before, after))
    result["projects"] = {**mix, "overlap": project_overlap(mix["before"], mix["after"]), "matched": bool(args.matched)}
    _utf8_stdout()
    since = min(r["first_ts"] or "" for r in after)
    guard_tokens = _guard_tokens(since)
    if args.json:
        print(json.dumps({**result, "guard_effect": guard_tokens}, ensure_ascii=False, indent=2))
        return 0
    labels = _event_labels()
    labelled = {labels.get(k, k): n for k, n in _event_counts(since).items()}
    names = (L("Without Service001", "Service001'siz"), L("With Service001", "Service001'li"))
    show = render_markdown if args.markdown else render
    print(show(result, labelled, labels=names, title_suffix=scope, guard_tokens=guard_tokens,
               own_startup=_own_startup(Path(args.root), args.project)))
    return 0


def _own_startup(root: Path, project: str | None) -> tuple[int, int] | None:
    """(tokens added by Service001, tokens of the first call) in the latest session, or None."""
    from .context_report import latest_transcript, measure

    try:
        path = latest_transcript(root, project)
        result = measure(path) if path else None
    except OSError:
        return None
    if not result:
        return None
    return sum(r["tokens"] for r in result["rows"] if r["owner"] == "service001"), result["first_call"]


def _history(args: argparse.Namespace) -> int:
    from .history import connect, render_history, sync

    con = connect()
    _utf8_stdout()
    if not args.no_sync:
        stats = sync(Path(args.root), con, fast=args.fast)
        if args.sync_only:
            return 0
        if stats["updated"]:
            print(L(f"{plural(stats['updated'], 'session')} added or updated in the history.",
                  f"{stats['updated']} oturum kayda eklendi veya güncellendi."))
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


def _context(args: argparse.Namespace) -> int:
    from .context_report import latest_transcript, measure, render

    path = latest_transcript(Path(args.root), args.project, args.session)
    _utf8_stdout()
    print(render(measure(path) if path else None))
    return 0 if path else 1


def _map(args: argparse.Namespace) -> int:
    from .project_map import build_map, ensure_map

    project = Path(args.project_dir or Path.cwd())
    _utf8_stdout()
    if args.announce:
        from .hooks import map_guidance

        path = ensure_map(project, force=True)
        if not path:
            print(L(f"{project} does not look like a project folder, so there is no map.",
                    f"{project} bir proje klasörü gibi görünmüyor, harita yok."))
            return 1
        print(map_guidance(path))
        print(L("The map and the index were just refreshed. Use them from now on in this session.",
                "Harita ve index şimdi yenilendi. Bu oturumda bundan sonra bunları kullanın."))
        return 0
    if args.graph:
        from .project_map import graph_data

        print(json.dumps(graph_data(project), ensure_ascii=False, indent=1))
        return 0
    if args.print:
        print(build_map(project))
        return 0
    path = ensure_map(project, force=True)
    if not path:
        print(L(f"{project} does not look like a project folder (no git, package.json, pyproject.toml, etc.).",
                f"{project} bir proje klasörü gibi görünmüyor (git, package.json, pyproject.toml vb. yok)."))
        return 1
    print(L(f"Project map written: {path}", f"Proje haritası yazıldı: {path}"))
    return 0


def _impact(args: argparse.Namespace) -> int:
    from . import code_graph
    from .code_index import load_project_config, scan
    from .project_map import list_files

    project = Path(args.project_dir or Path.cwd())
    _utf8_stdout()
    files = list_files(project)
    target = Path(args.file).as_posix() if not Path(args.file).is_absolute() else None
    if target is None:
        try:
            target = Path(args.file).resolve().relative_to(project.resolve()).as_posix()
        except ValueError:
            target = Path(args.file).as_posix()
    target = target.removeprefix("./")
    if target not in files:
        matches = [f for f in files if f.endswith("/" + target)]
        if len(matches) != 1:
            print(L(f"File not found in the project: {args.file}" if not matches else
                    f"{args.file} matches several files: {', '.join(matches[:5])}",
                    f"Dosya projede bulunamadı: {args.file}" if not matches else
                    f"{args.file} birden fazla dosyayla eşleşiyor: {', '.join(matches[:5])}"))
            return 1
        target = matches[0]
    idx = scan(project, files, load_project_config(project))
    hit = code_graph.impacted(idx.importers, target, args.depth)
    print(L(f"{target} imports {len(idx.imports.get(target, []))} project files and is imported by "
            f"{len(idx.importers.get(target, []))}.",
            f"{target} projede {len(idx.imports.get(target, []))} dosyayı içe aktarıyor, "
            f"{len(idx.importers.get(target, []))} dosya tarafından içe aktarılıyor."))
    if not hit:
        print(L("No other file depends on it (static imports only).", "Ona bağlı başka dosya yok (yalnızca statik import'lar)."))
        return 0
    print(L(f"Affected by a change, up to {args.depth} steps away ({len(hit)} files):",
            f"Değişiklikten etkilenenler, en fazla {args.depth} adım uzakta ({len(hit)} dosya):"))
    for f, distance in hit[:args.limit]:
        print(f"  {distance}  {f}")
    if len(hit) > args.limit:
        print(L(f"  ... {len(hit) - args.limit} more", f"  ... {len(hit) - args.limit} dosya daha"))
    print(L("Static imports only: dynamic imports and same-package references are not seen.",
            "Yalnızca statik import'lar: dinamik import'lar ve aynı paket içi referanslar görülmez."))
    return 0


# Kept here, not read from hooks.HANDLERS, so that building the parser does not import the hook code.
# tests/test_hook_events.py checks that it matches the handlers and the events the installer registers.
HOOK_EVENTS = ["session-start", "user-prompt-submit", "pre-tool-use", "stop", "session-end"]


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
    from .installer import InstallError, install, settings_path, uninstall, verify_python

    path = settings_path(args.scope, args.project_dir)
    try:
        if not remove:
            verify_python(sys.executable)  # the hooks will run this interpreter
        if remove:
            settings, notes = uninstall(path, dry_run=args.dry_run)
        else:
            settings, notes = install(
                path, statusline=args.statusline, compact_window=args.compact_window,
                bash_output_max=args.bash_output_max, agents=args.agents,
                subagent_model=args.subagent_model, model=args.model, dry_run=args.dry_run,
            )
    except InstallError as exc:
        print(L(f"Error: {exc}", f"Hata: {exc}"), file=sys.stderr)
        return 1
    _utf8_stdout()
    print(L(f"Settings file: {path}", f"Ayar dosyası: {path}"))
    for note in notes:
        print(f"- {note}")
    if args.dry_run:
        print(L("Dry run: no file was changed. Content that would be written:",
                "Deneme modu: dosya değiştirilmedi. Yazılacak içerik:"))
        print(json.dumps(settings, indent=2, ensure_ascii=False))
    else:
        print(L("Takes effect in new sessions. In an open session, opening the /hooks menu once reloads the settings.",
                "Yeni oturumlarda geçerli olur. Açık bir oturumda /hooks menüsünü bir kez açmak ayarları yeniden yükler."))
    return 0


def _event_labels() -> dict[str, str]:
    return {
        "context_warning": L("Context length warning", "Bağlam uzunluğu uyarısı"),
        "write_rewrite": L("Full rewrite with Write warning", "Write ile baştan yazma uyarısı"),
        "write_rewrite_blocked": L("Full rewrite with Write blocked", "Write ile baştan yazma engeli"),
        "write_rewrite_retried": L("Retry allowed after block", "Engelden sonra izin verilen tekrar"),
        "heredoc_rewrite": L("Full rewrite with heredoc warning", "Heredoc ile baştan yazma uyarısı"),
        "read_large_blocked": L("Reading a whole large file blocked", "Büyük dosyanın tamamını okuma engeli"),
        "read_large_retried": L("Full read allowed after block", "Engelden sonra izin verilen tam okuma"),
        "read_large": L("Reading a whole large file warning", "Büyük dosyanın tamamını okuma uyarısı"),
        "shell_read_blocked": L("Dumping a large file in the shell blocked", "Kabukta büyük dosya basma engeli"),
        "shell_read_retried": L("File dump allowed after block", "Engelden sonra izin verilen dosya basma"),
        "shell_read": L("Dumping a large file in the shell warning", "Kabukta büyük dosya basma uyarısı"),
        "model_advice": L("Model advice requested on the first prompt", "İlk promptta model önerisi istendi"),
        "routing_nudge": L("Reminder to delegate exploration to the scout (Haiku)", "Keşfi scout'a (Haiku) devretme hatırlatması"),
        "subagent_call": L("Subagent call", "Alt ajan çağrısı"),
    }


def _guard_tokens(since: str) -> dict[str, int]:
    """Tokens that blocking guards kept out of the context since `since`, from the event log (upper bound)."""
    from .guard_config import home

    out = {"blocked": 0, "blocked_tokens": 0, "retried_tokens": 0}
    try:
        lines = (home() / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("ts", "") < since:
            continue
        tokens = int(event.get("est_tokens") or 0)
        if event.get("kind") in ("read_large_blocked", "shell_read_blocked"):
            out["blocked"] += 1
            out["blocked_tokens"] += tokens
        elif event.get("kind") == "shell_read_retried":
            out["retried_tokens"] += tokens
    return out


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
    labels = _event_labels()
    _utf8_stdout()
    print(L(f"Guard events in the last {args.days:g} days:", f"Son {args.days:g} günde koruyucu olayları:"))
    if not kinds:
        print(L("- no records", "- kayıt yok"))
    for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"- {labels.get(kind, kind)}: {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cc-optimizer", description="Claude Code token analysis and guards")
    parser.add_argument("--version", action="version", version=f"Service001 {__version__}")
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
    hk.add_argument("event", choices=HOOK_EVENTS)
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
    hs.add_argument("--project", default=None, help="project name or part of it, e.g. MYAPP")
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
    mp.add_argument("--announce", action="store_true",
                    help="refresh the map and index, then print how to use them (for the /project-map command)")
    mp.add_argument("--graph", action="store_true",
                    help="print the code graph (files, imports, importers, symbols, routes) as JSON")
    mp.set_defaults(func=_map)

    ip = sub.add_parser("impact", help="list the files that depend on a file, directly or through other files")
    ip.add_argument("file", help="file path (relative to the project, or just its name when unique)")
    ip.add_argument("--project-dir", default=None, help="project folder (default: current)")
    ip.add_argument("--depth", type=int, default=3, help="how many import steps to follow (default 3)")
    ip.add_argument("--limit", type=int, default=40, help="most files to list (default 40)")
    ip.set_defaults(func=_impact)

    cp = sub.add_parser("compare", help="compare sessions after installing the guards with the baseline")
    cp.add_argument("--baseline", default=None,
                    help="compare transcripts with this analyze --json file instead of using the history")
    cp.add_argument("--since", default=None, help="with --baseline: sessions started at or after this date")
    cp.add_argument("--root", default=str(default_root()))
    cp.add_argument("--project", default=None, help="project name or part of it, e.g. MYAPP")
    cp.add_argument("--fast", action="store_true")
    cp.add_argument("--json", action="store_true")
    cp.add_argument("--matched", action="store_true",
                    help="compare only projects that have sessions both before and after (like with like)")
    cp.add_argument("--markdown", action="store_true", help="print the report as a Markdown table")
    cp.set_defaults(func=_compare)

    cx = sub.add_parser("context", help="what makes up the fixed load at the start of a session")
    cx.add_argument("--root", default=str(default_root()))
    cx.add_argument("--project", default=None, help="project folder or part of its name (default: any project)")
    cx.add_argument("--session", default=None, help="session id or its start (default: the latest session)")
    cx.set_defaults(func=_context)

    ev = sub.add_parser("events", help="what the guards did")
    ev.add_argument("--days", type=float, default=7.0)
    ev.set_defaults(func=_events)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
