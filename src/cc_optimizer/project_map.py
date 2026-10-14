"""Compact project map, so Claude does not rediscover the layout in every session.

The map is a file in the project (.claude/project-map.md). Only its path is added to the
context at session start: content added to the context is re-read on every call, while a
file is read once, when needed. It is regenerated when it is missing, older than a few
hours, or when the git HEAD moved. It is kept out of git through .git/info/exclude.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

MAP_NAME = "project-map.md"
STALE_SECONDS = 6 * 3600
MAX_LINES = 200
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
    """Project files as posix paths, honoring .gitignore when the project is a git repository."""
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
            lines.append(f"{'  ' * depth}{d.rsplit('/', 1)[-1]}/ {count}{kinds}")
        return lines

    modes = ([files_mode] if describe_files else []) + [dirs_mode]
    for mode in modes:
        for max_depth in range(8, 0, -1):
            lines = mode(max_depth)
            if len(lines) <= MAX_LINES:
                return lines
    return dirs_mode(1)[:MAX_LINES] + ["... (truncated)"]


def build_map(project: Path) -> str:
    files = list_files(project)
    head = git_head(project)
    out = [
        f"<!-- cc-optimizer project map | head={head or '-'} | files={len(files)} -->",
        f"# Project map: {project.resolve().name}",
        "",
        "Generated by cc-optimizer from the file tree. Layout only: verify details in the code before relying on them.",
        "",
    ]
    summary = _readme_summary(project)
    if summary:
        out += ["## README", summary, ""]
    manifests = _manifests(project, files)
    if manifests:
        out += ["## Manifests and scripts", *manifests, ""]
    out += [f"## Layout ({len(files)} files)", "```", *_tree(project, files, len(files) <= DESCRIBE_MAX_FILES), "```"]
    return "\n".join(out) + "\n"


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


def is_stale(project: Path) -> bool:
    path = map_path(project)
    try:
        if time.time() - path.stat().st_mtime > STALE_SECONDS:
            return True
        with path.open(encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return True
    head = git_head(project)
    return bool(head) and f"head={head}" not in first


def ensure_map(project: Path, force: bool = False) -> Path | None:
    """Create or refresh the map. Returns its path, or None when this is not a project folder."""
    if not is_project(project):
        return None
    path = map_path(project)
    if force or is_stale(project):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_map(project), encoding="utf-8")
        exclude_from_git(project)
    return path
