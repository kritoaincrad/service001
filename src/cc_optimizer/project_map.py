"""Compact project map, so Claude does not rediscover the layout in every session.

The map is a file in the project (.claude/project-map.md), next to a grep-able index
(.claude/project-index.md) of routes, pages and symbols. Only the map path is added to the
context at session start: content added to the context is re-read on every call, while a
file is read once, when needed. Both are regenerated when missing, older than a few hours,
when the git HEAD moved, or when files were added, removed or renamed. They are kept out of
git through .git/info/exclude.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

from .code_index import CodeIndex, Route, load_project_config, scan

MAP_NAME = "project-map.md"
INDEX_NAME = "project-index.md"
STALE_SECONDS = 6 * 3600
MAX_LINES = 200
MAX_PAGE_LINES = 100
MAX_PAGE_FILES = 60
MAX_API_LINES = 60
MAX_ENTRY_POINTS = 12
MAX_INDEX_ROWS = 8000
MAX_HUBS = 10
MAX_DEP_LIST = 8
FOLDER_INTRO_FILES = ("__init__.py", "index.ts", "index.tsx", "index.js", "mod.rs", "main.py", "main.go")
MAX_FILES = 20_000
DESCRIBE_MAX_FILES = 150  # one-line file descriptions only for small projects
LIST_MAX_FILES = 10  # large projects: root files are listed only up to this many
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "out", ".next", ".nuxt",
    "target", "bin", "obj", ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage",
    ".turbo", ".cache", "vendor", ".gradle", ".terraform", ".claude", "test-results", "playwright-report",
    "logs", "tmp", "temp",
}
MARKERS = (".git", "package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml", "pom.xml",
           "build.gradle", "composer.json", "Gemfile", "pubspec.yaml")
MARKER_GLOBS = ("*.sln", "*.csproj")
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs", ".java", ".kt", ".cs", ".rb",
                   ".php", ".swift", ".dart", ".vue", ".svelte", ".sh", ".ps1", ".sql"}
_COMMENT_OPEN = re.compile(r'^(?:"""|\'\'\'|/\*\*?|//+|#+|--|<!--)\s*')
_COMMENT_CLOSE = re.compile(r'\s*(?:"""|\'\'\'|\*/|-->)\s*$')
_SKIP_PREFIXES = ("#!", "# -*-", "from __future__", "'use ", '"use ', "// @ts-", "// eslint", "/* eslint")


def map_path(project: Path) -> Path:
    return project / ".claude" / MAP_NAME


def index_path(project: Path) -> Path:
    return project / ".claude" / INDEX_NAME


def is_project(path: Path) -> bool:
    try:
        if not path.is_dir() or path.resolve() == Path.home().resolve():
            return False
        return any((path / m).exists() for m in MARKERS) or any(next(path.glob(g), None) for g in MARKER_GLOBS)
    except OSError:
        return False


def git_head(project: Path) -> str | None:
    """Current commit without running git: .git/HEAD, then the ref file or packed-refs."""
    git = project / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref = head[4:].strip()
        if (git / ref).is_file():
            return (git / ref).read_text(encoding="utf-8").strip()
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        return None
    return None


def list_files(project: Path) -> list[str]:
    """Project files as posix paths, honoring .gitignore and the project's `ignore` setting."""
    cfg = load_project_config(project)
    files = _all_files(project)
    return [f for f in files if not cfg.ignored(f)] if cfg.ignore else files


def _all_files(project: Path) -> list[str]:
    if (project / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(project), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                capture_output=True, timeout=5, check=True,
            ).stdout.decode("utf-8", "replace")
            files = [f for f in out.split("\0") if f and not any(p in SKIP_DIRS for p in f.split("/")[:-1])]
            return sorted(files)[:MAX_FILES]
        except (OSError, subprocess.SubprocessError):
            pass
    files: list[str] = []
    for root, dirs, names in os.walk(project):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel = Path(root).relative_to(project).as_posix()
        files += [n if rel == "." else f"{rel}/{n}" for n in sorted(names)]
        if len(files) >= MAX_FILES:
            break
    return files[:MAX_FILES]


def describe(path: Path) -> str:
    """First line of a source file's leading docstring or comment, or ''."""
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        return ""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            head = [fh.readline() for _ in range(8)]
    except OSError:
        return ""
    for line in head:
        text = line.strip()
        if not text or text.startswith(_SKIP_PREFIXES):
            continue
        if not _COMMENT_OPEN.match(text):
            return ""  # code before any comment: no description
        text = _COMMENT_CLOSE.sub("", _COMMENT_OPEN.sub("", text)).strip(" *")
        return text[:90] if len(text) > 3 else ""
    return ""


def _readme_summary(project: Path) -> str:
    for name in ("README.md", "readme.md", "README.rst", "README.txt", "README"):
        try:
            lines = (project / name).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        para: list[str] = []
        for line in lines:
            text = line.strip()
            if not text or text.startswith(("#", "![", "[!", "<", "```", "---", "===")):
                if para:
                    break
                continue
            para.append(text)
        return " ".join(para)[:400]
    return ""


def _manifests(project: Path, files: list[str]) -> list[str]:
    out: list[str] = []
    for rel in [f for f in files if f.count("/") <= 2 and f.rsplit("/", 1)[-1] == "package.json"][:6]:
        try:
            data = json.loads((project / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
        listed = ", ".join(f"{k}: {str(v)[:60]}" for k, v in list(scripts.items())[:8])
        out.append(f"- {rel} ({data.get('name', '?')})" + (f" scripts: {listed}" if listed else ""))
    if "pyproject.toml" in files:
        try:
            import tomllib

            data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
            proj = data.get("project", {})
            scripts = ", ".join(f"{k} = {v}" for k, v in (proj.get("scripts") or {}).items())
            out.append(f"- pyproject.toml ({proj.get('name', '?')})" + (f" scripts: {scripts}" if scripts else ""))
        except (OSError, ValueError, ImportError):
            pass
    for name in ("requirements.txt", "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "composer.json", "Gemfile",
                 "Makefile", "docker-compose.yml", "Dockerfile"):
        if name in files:
            out.append(f"- {name}")
    return out


def _tree(project: Path, files: list[str], describe_files: bool) -> list[str]:
    direct: dict[str, list[str]] = defaultdict(list)
    total: Counter = Counter()
    for f in files:
        parent, _, name = f.rpartition("/")
        direct[parent].append(name)
        parts = parent.split("/") if parent else []
        for i in range(len(parts) + 1):
            total["/".join(parts[:i])] += 1
    dirs = sorted(total, key=lambda d: d.split("/") if d else [])

    def files_mode(max_depth: int) -> list[str]:
        """Every file, with its description: for small projects."""
        lines: list[str] = []
        for d in dirs:
            depth = d.count("/") + 1 if d else 0
            if depth > max_depth:
                continue
            indent = "  " * depth
            lines.append(f"{indent}{d.rsplit('/', 1)[-1]}/" if d else "./")
            for name in direct.get(d, []):
                desc = describe(project / (f"{d}/{name}" if d else name)) if describe_files else ""
                lines.append(f"{indent}  {name}" + (f" - {desc}" if desc else ""))
            if depth == max_depth and total[d] > len(direct.get(d, [])):
                lines.append(f"{indent}  ... {total[d] - len(direct.get(d, []))} more files in subfolders")
        return lines

    def dirs_mode(max_depth: int) -> list[str]:
        """Directories with recursive file counts and main file types: for large projects."""
        root_files = direct.get("", [])
        lines = [f"./ {total['']} files"] + (
            [f"  {n}" for n in root_files] if len(root_files) <= LIST_MAX_FILES else []
        )
        for d in dirs:
            depth = d.count("/") + 1 if d else 0
            if not d or depth > max_depth:
                continue
            exts = Counter(Path(n).suffix or n for n in direct.get(d, [])).most_common(3)
            kinds = f" ({', '.join(f'{x} {c}' for x, c in exts)} here)" if exts else ""
            count = f"{total[d]} file" + ("" if total[d] == 1 else "s")
            about = next((x for n in direct.get(d, []) if n in FOLDER_INTRO_FILES
                          and (x := describe(project / d / n))), "")
            lines.append(f"{'  ' * depth}{d.rsplit('/', 1)[-1]}/ {count}{kinds}" + (f" - {about}" if about else ""))
        return lines

    modes = ([files_mode] if describe_files else []) + [dirs_mode]
    for mode in modes:
        for max_depth in range(8, 0, -1):
            lines = mode(max_depth)
            if len(lines) <= MAX_LINES:
                return lines
    return dirs_mode(1)[:MAX_LINES] + ["... (truncated)"]


def tree_fingerprint(project: Path, files: list[str]) -> str:
    """Changes when a file is added, removed or renamed or the project settings change, so new pages show up
    without waiting for a commit."""
    text = "\0".join(files) + "\1" + load_project_config(project).raw
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]


def _route_line(r: Route) -> str:
    target = r.file + (f":{r.line}" if r.line > 1 else "")
    text = f"{r.path}  {target}"
    if r.name:
        text += f" ({r.name})"
    if r.via:
        text += f" <- route declared in {r.via}"
    if r.uses:
        text += f" | uses: {', '.join(r.uses)}"
    return text


def _api_lines(routes: list[Route], limit: int) -> list[str]:
    by_file: dict[str, list[str]] = defaultdict(list)
    for r in routes:
        label = f"{r.method} {r.path}".strip()
        by_file[r.file].append(label + (f" ({r.name})" if r.name else ""))
    lines = []
    for file, labels in by_file.items():
        text = f"{file}: {', '.join(labels)}"
        lines.append(text if len(text) <= 260 else text[:257] + "...")
    if len(lines) > limit:
        lines = lines[:limit] + [f"... {len(by_file) - limit} more files with routes (grep {INDEX_NAME})"]
    return lines


def _short_list(items: list[str], limit: int = MAX_DEP_LIST) -> str:
    """File names for a one-line list; parent/name when two files share a name (index.ts, __init__.py)."""
    shown = items[:limit]
    base = Counter(i.rsplit("/", 1)[-1] for i in shown)
    names = ["/".join(i.rsplit("/", 2)[-2:]) if base[i.rsplit("/", 1)[-1]] > 1 else i.rsplit("/", 1)[-1] for i in shown]
    return ", ".join(names) + (f", +{len(items) - limit} more" if len(items) > limit else "")


def _hub_section(idx: CodeIndex) -> list[str]:
    """Where to look first: the files most others depend on, and the symbols most files mention."""
    files = sorted(((f, u) for f, u in idx.importers.items() if len(u) >= 2),
                   key=lambda kv: (-len(kv[1]), kv[0]))[:MAX_HUBS]
    symbols = sorted(((n, c) for n, c in idx.refs.items() if not n.isupper()),  # skip CONSTANTS: not entry points
                     key=lambda kv: (-kv[1], kv[0]))[:MAX_HUBS]
    where = {name: f"{file}:{line}" for file, line, _, name in idx.symbols}
    if not files and not symbols:
        return []
    out = ["## Hubs: a change here reaches the most code"]
    if files:
        out += ["Files by number of importing files:", "```", *(f"{f}  imported by {len(users)}" for f, users in files), "```"]
    if symbols:
        out += ["Symbols by number of other files that mention them:", "```",
                *(f"{n}  {where.get(n, '?')}  in {c} files" for n, c in symbols), "```"]
    return out + [""]


def _find_sections(idx: CodeIndex) -> list[str]:
    """Pages, server routes and entry points: the answers to 'where is X' that otherwise cost 5-6 searches."""
    out: list[str] = []
    if idx.entry_points:
        out += ["## Entry points", ", ".join(idx.entry_points[:MAX_ENTRY_POINTS]), ""]
    out += _hub_section(idx)
    pages = sorted((r for r in idx.routes if r.kind == "page"), key=lambda r: (r.path, r.file))
    if pages:
        out += [f"## Pages / UI routes ({len(pages)}): route  file (component) | uses",
                "Route paths are as written; nested routes are not joined to their parent.", "```",
                *(_route_line(r) for r in pages[:MAX_PAGE_LINES])]
        if len(pages) > MAX_PAGE_LINES:
            out.append(f"... {len(pages) - MAX_PAGE_LINES} more (grep {INDEX_NAME})")
        out += ["```", ""]
    if idx.page_files:
        out += [f"## Page / view files with no detected route ({len(idx.page_files)})", "```",
                *idx.page_files[:MAX_PAGE_FILES]]
        if len(idx.page_files) > MAX_PAGE_FILES:
            out.append(f"... {len(idx.page_files) - MAX_PAGE_FILES} more (grep {INDEX_NAME})")
        out += ["```", ""]
    api = [r for r in idx.routes if r.kind == "api"]
    if api:
        out += [f"## Server routes / API endpoints ({len(api)}): file: METHOD path (handler)", "```",
                *_api_lines(api, MAX_API_LINES), "```", ""]
    return out


def render_map(project: Path, files: list[str], idx: CodeIndex) -> str:
    head = git_head(project)
    out = [
        f"<!-- cc-optimizer project map | head={head or '-'} | files={len(files)} | tree={tree_fingerprint(project, files)} -->",
        f"# Project map: {project.resolve().name}",
        "",
        "Generated by cc-optimizer from the file tree. Layout only: verify details in the code before relying on them.",
        f"To find where something is, grep {INDEX_NAME} (same folder): it lists every route, page and top-level "
        "symbol as `name  file:line  kind`.",
        "",
    ]
    summary = _readme_summary(project)
    if summary:
        out += ["## README", summary, ""]
    manifests = _manifests(project, files)
    if manifests:
        out += ["## Manifests and scripts", *manifests, ""]
    out += _find_sections(idx)
    out += [f"## Layout ({len(files)} files)", "```", *_tree(project, files, len(files) <= DESCRIBE_MAX_FILES), "```"]
    return "\n".join(out) + "\n"


def render_index(project: Path, files: list[str], idx: CodeIndex) -> str:
    """One line per route, page and symbol, name first, so a single grep answers 'where is X'."""
    head = git_head(project)
    rows: list[tuple[str, str, str, str]] = []
    for r in idx.routes:
        target = r.file + (f":{r.line}" if r.line > 1 else "")
        kind = "page" if r.kind == "page" else f"{r.method or 'route'} api"
        rows.append((r.path, target, kind, r.name))
    rows += [(f, f, "page-file", "") for f in idx.page_files]
    rows += [(name, f"{file}:{line}", kind, f"refs {idx.refs[name]}" if name in idx.refs else "")
             for file, line, kind, name in idx.symbols]
    for file in sorted(set(idx.imports) | set(idx.importers)):
        uses, users = idx.imports.get(file, []), idx.importers.get(file, [])
        parts = []
        if users:
            parts.append(f"used by {len(users)}: {_short_list(users)}")
        if uses:
            parts.append(f"imports {len(uses)}: {_short_list(uses)}")
        rows.append((file, file, "deps", "; ".join(parts)))
    rows.sort(key=lambda row: (row[1].split(":")[0], row[0]))
    out = [
        f"<!-- cc-optimizer project map index | head={head or '-'} | tree={tree_fingerprint(project, files)} -->",
        "# Project index: name  file:line  kind",
        "Routes (page / METHOD api), view files and top-level symbols. Grep a name, a URL part or a file, e.g. "
        f"`grep -i orders {INDEX_NAME}`. Generated by cc-optimizer; verify in the code.",
        "",
    ]
    for name, target, kind, extra in rows[:MAX_INDEX_ROWS]:
        out.append(f"{name}  {target}  {kind}" + (f"  ({extra})" if extra else ""))
    if len(rows) > MAX_INDEX_ROWS:
        out.append(f"... {len(rows) - MAX_INDEX_ROWS} more rows not listed")
    return "\n".join(out) + "\n"


def build(project: Path, files: list[str] | None = None) -> tuple[str, str]:
    """The map and the index texts."""
    files = list_files(project) if files is None else files
    idx = scan(project, files, load_project_config(project))
    return render_map(project, files, idx), render_index(project, files, idx)


def graph_data(project: Path) -> dict:
    """The whole index as plain JSON (files with their imports, importers and symbols, plus routes), for any
    visualizer or script. Language-independent."""
    files = list_files(project)
    idx = scan(project, files, load_project_config(project))
    symbols: dict[str, list[dict]] = defaultdict(list)
    for file, line, kind, name in idx.symbols:
        symbols[file].append({"name": name, "line": line, "kind": kind, "refs": idx.refs.get(name, 0)})
    nodes = sorted(set(idx.imports) | set(idx.importers) | set(symbols) | {r.file for r in idx.routes})
    return {
        "project": project.resolve().name,
        "files": [{"path": f, "imports": idx.imports.get(f, []), "used_by": idx.importers.get(f, []),
                   "symbols": symbols.get(f, [])} for f in nodes],
        "routes": [{"kind": r.kind, "method": r.method, "path": r.path, "file": r.file, "line": r.line, "name": r.name}
                   for r in idx.routes],
    }


def build_map(project: Path) -> str:
    return build(project)[0]


def exclude_from_git(project: Path, entry: str = f".claude/{MAP_NAME}") -> None:
    """Keep a generated file out of git through .git/info/exclude, which is never shared."""
    exclude = project / ".git" / "info" / "exclude"
    try:
        if not (project / ".git").is_dir():
            return
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if entry not in current.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            sep = "" if not current or current.endswith("\n") else "\n"
            exclude.write_text(current + sep + entry + "\n", encoding="utf-8")
    except OSError:
        pass


def is_stale(project: Path, files: list[str] | None = None) -> bool:
    path = map_path(project)
    try:
        if time.time() - path.stat().st_mtime > STALE_SECONDS:
            return True
        with path.open(encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return True
    if not index_path(project).is_file():
        return True
    head = git_head(project)
    if head and f"head={head}" not in first:
        return True
    files = list_files(project) if files is None else files
    return f"tree={tree_fingerprint(project, files)}" not in first


def ensure_map(project: Path, force: bool = False) -> Path | None:
    """Create or refresh the map and the index. Returns the map path, or None when this is not a project folder."""
    if not is_project(project):
        return None
    path = map_path(project)
    files = list_files(project)
    if force or is_stale(project, files):
        path.parent.mkdir(parents=True, exist_ok=True)
        map_text, index_text = build(project, files)
        path.write_text(map_text, encoding="utf-8")
        index_path(project).write_text(index_text, encoding="utf-8")
        exclude_from_git(project)
        exclude_from_git(project, f".claude/{INDEX_NAME}")
    return path
