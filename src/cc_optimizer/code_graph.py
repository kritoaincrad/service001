"""Dependency edges and reference counts: which files a file imports, who imports it, and which symbols the
rest of the project uses most.

This is the part of a code knowledge graph that pays off for an AI coding agent: "what breaks if I change this
file" and "where should I look first". It is kept as plain data and text (no database, no server), so it works
in any project at once. Like the rest of the index it is heuristic: static imports only. Dynamic imports built
at run time, reflection, and same-package references that need no import statement are not seen.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

RESOLVE_SUFFIXES = (".tsx", ".ts", ".jsx", ".js", ".mjs", ".vue", ".svelte")
CODE_SUFFIXES = set(RESOLVE_SUFFIXES) | {".cjs", ".py", ".java", ".kt"}
MIN_REF_NAME = 4  # shorter names (get, run, id) are too common to count as references
MAX_CONFIGS = 20
MAX_DEFINITIONS = 3  # a name defined in more files than this is generic (main, index, handler)
STOP_NAMES = {"main", "init", "test", "self", "this", "None", "True", "False", "null", "true", "false", "index",
              "default", "handler", "value", "name", "data", "type", "util", "utils", "config", "setup"}

_JS_IMPORT = re.compile(r"""\bimport\s+(?:[^;'"]*?\s+from\s+)?["']([^"']+)["']""")
_JS_EXPORT_FROM = re.compile(r"""\bexport\s+(?:\*|\{[^}]*\})(?:\s+as\s+\w+)?\s+from\s+["']([^"']+)["']""")
_JS_REQUIRE = re.compile(r"""\b(?:require|import)\(\s*["']([^"']+)["']\s*\)""")
_JVM_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;?\s*$", re.M)
_IDENT = re.compile(r"[A-Za-z_$][\w$]{3,}")


def _load_jsonc(text: str) -> dict:
    """tsconfig.json allows comments and trailing commas."""
    try:
        return json.loads(text)
    except ValueError:
        stripped = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", text, flags=re.S)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        try:
            data = json.loads(stripped)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


class FileIndex(set):
    """The project's file paths, plus the import aliases of its tsconfig.json / jsconfig.json files."""

    def __init__(self, files: list[str], project: Path | None = None) -> None:
        super().__init__(files)
        self.top_dirs = {f.split("/")[0] for f in files if "/" in f}
        self.configs: list[tuple[str, str, dict[str, list[str]]]] = []  # folder, baseUrl folder, paths
        if project is None:
            return
        found = [f for f in files if f.rsplit("/", 1)[-1] in ("tsconfig.json", "jsconfig.json")
                 and "node_modules" not in f][:MAX_CONFIGS]
        for rel in found:
            try:
                options = _load_jsonc((project / rel).read_text(encoding="utf-8")).get("compilerOptions") or {}
            except OSError:
                continue
            folder = posixpath.dirname(rel)
            base = posixpath.normpath(posixpath.join(folder, options.get("baseUrl") or ".")) if "baseUrl" in options else folder
            paths = options.get("paths") if isinstance(options.get("paths"), dict) else {}
            if "baseUrl" in options or paths:
                self.configs.append((folder, "" if base == "." else base, {k: v for k, v in paths.items() if isinstance(v, list)}))
        self.configs.sort(key=lambda c: -len(c[0]))  # the nearest config first

    def alias_bases(self, spec: str, rel: str) -> list[str]:
        """Candidate project paths for a bare specifier, from tsconfig `paths` / `baseUrl`, or `src/...` style roots."""
        bases: list[str] = []
        for folder, base, paths in self.configs:
            if folder and not rel.startswith(folder + "/"):
                continue
            for pattern, targets in paths.items():
                prefix, star, suffix = pattern.partition("*")
                if star and spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix + suffix):
                    rest = spec[len(prefix):len(spec) - len(suffix)]
                    bases += [posixpath.normpath(posixpath.join(base, t.replace("*", rest))) for t in targets]
                elif not star and spec == pattern:
                    bases += [posixpath.normpath(posixpath.join(base, t)) for t in targets]
            bases.append(posixpath.normpath(posixpath.join(base, spec)))  # baseUrl-relative import
        if spec.split("/")[0] in self.top_dirs:
            bases.append(spec)  # `import x from "src/utils/x"` with the project root as the base
        return bases


def resolve_js(spec: str, rel: str, fileset: set[str]) -> str | None:
    """A JS/TS import specifier to a project file: relative paths, `@/` and `~/` aliases, tsconfig paths."""
    if spec.startswith("."):
        bases = [posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))]
    else:
        bases = (["src/" + spec[2:], spec[2:]] if spec.startswith(("@/", "~/", "#/")) else [])
        if isinstance(fileset, FileIndex):
            bases += fileset.alias_bases(spec, rel)
    for base in bases:
        if base in fileset:
            return base
        for ext in RESOLVE_SUFFIXES:
            for cand in (base + ext, f"{base}/index{ext}"):
                if cand in fileset:
                    return cand
    return None


class Resolver:
    """Maps dotted module / class names to files for Python, Java and Kotlin, by path suffix.

    A name that matches more than one file is dropped rather than guessed.
    """

    def __init__(self, files: list[str] | FileIndex) -> None:
        self.fileset = files if isinstance(files, FileIndex) else FileIndex(files)
        self.modules: dict[str, str | None] = {}
        for f in files:
            if not f.endswith((".py", ".java", ".kt")):
                continue
            parts = f.rsplit(".", 1)[0].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for i in range(len(parts)):
                name = ".".join(parts[i:])
                self.modules[name] = None if name in self.modules and self.modules[name] != f else f

    def module_file(self, dotted: str) -> str | None:
        """Longest known prefix: `a.b.c` may be a module a.b with an attribute c."""
        names = dotted.split(".")
        for k in range(len(names), 0, -1):
            found = self.modules.get(".".join(names[:k]))
            if found:
                return found
        return None

    def path_file(self, path: str) -> str | None:
        for cand in (path + ".py", f"{path}/__init__.py"):
            if cand in self.fileset:
                return cand
        return None


def _py_imports(rel: str, text: str, res: Resolver) -> set[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    package = rel.split("/")[:-1]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in sys.stdlib_module_names and (f := res.module_file(alias.name)):
                    out.add(f)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    continue
                base = "/".join(package[:keep] + (node.module.split(".") if node.module else []))
                module = res.path_file(base) if base else None
                for alias in node.names:
                    sub = res.path_file(f"{base}/{alias.name}" if base else alias.name)
                    if target := sub or module:
                        out.add(target)
            else:
                name = node.module or ""
                if not name or name.split(".")[0] in sys.stdlib_module_names:
                    continue
                module = res.module_file(name)
                for alias in node.names:
                    if target := res.modules.get(f"{name}.{alias.name}") or module:
                        out.add(target)
    out.discard(rel)
    return out


def imports_of(rel: str, suffix: str, text: str, res: Resolver) -> list[str]:
    """Project files that `rel` imports, sorted."""
    found: set[str] = set()
    if suffix in RESOLVE_SUFFIXES or suffix == ".cjs":
        specs = set(_JS_IMPORT.findall(text)) | set(_JS_EXPORT_FROM.findall(text)) | set(_JS_REQUIRE.findall(text))
        found = {t for s in specs if (t := resolve_js(s, rel, res.fileset))}
    elif suffix == ".py":
        found = _py_imports(rel, text, res)
    elif suffix in (".java", ".kt"):
        found = {t for s in _JVM_IMPORT.findall(text) if (t := res.module_file(s))}
    found.discard(rel)
    return sorted(t for t in found if Path(t).suffix.lower() in CODE_SUFFIXES)


def reverse(imports: dict[str, list[str]]) -> dict[str, list[str]]:
    """File -> files that import it, sorted."""
    out: dict[str, set[str]] = defaultdict(set)
    for src, targets in imports.items():
        for target in targets:
            out[target].add(src)
    return {k: sorted(v) for k, v in out.items()}


def impacted(importers: dict[str, list[str]], start: str, depth: int = 3) -> list[tuple[str, int]]:
    """Files that would be affected by a change to `start`, with their distance: importers, then their importers."""
    seen = {start: 0}
    queue = [start]
    while queue:
        current = queue.pop(0)
        if seen[current] >= depth:
            continue
        for nxt in importers.get(current, []):
            if nxt not in seen:
                seen[nxt] = seen[current] + 1
                queue.append(nxt)
    return sorted(((f, d) for f, d in seen.items() if f != start), key=lambda x: (x[1], x[0]))


def count_references(project: Path, files: list[str], definitions: dict[str, set[str]], read, deadline: float) -> Counter:
    """For each defined symbol name: in how many other files does the name appear (an Aider-style ranking).

    `definitions` maps a symbol name to the files that define it. Generic and very short names are skipped.
    """
    wanted = {n: d for n, d in definitions.items()
              if len(n) >= MIN_REF_NAME and n not in STOP_NAMES and len(d) <= MAX_DEFINITIONS}
    refs: Counter = Counter()
    for rel in files:
        if time.monotonic() > deadline:
            break
        text = read(project / rel)
        if text is None:
            continue
        for name in set(_IDENT.findall(text)) & wanted.keys():
            if rel not in wanted[name]:
                refs[name] += 1
    return refs


# ---- languages that import a package or namespace, not a file ---------------------------------------------------
# Go, Java/Kotlin (same package) and C# connect files through shared scopes. An edge is drawn when a file mentions
# a type (or an exported Go function) that is defined, in only one place, in a scope the file can see.

TYPE_KINDS = {"class", "interface", "enum", "record", "struct", "trait", "object", "type"}
SCOPE_SUFFIXES = (".go", ".java", ".kt", ".cs")
_CS_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
_CS_USING = re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?([\w.]+)\s*;", re.M)
_GO_MODULE = re.compile(r"^module\s+(\S+)", re.M)
_GO_IMPORT = re.compile(r"\bimport\s*(?:\(([^)]*)\)|(?:\w+\s+|\.\s+)?\"([^\"]+)\")")
_QUOTED = re.compile(r"\"([^\"]+)\"")


def _scope_symbols(symbols: list[tuple[str, int, str, str]]) -> list[tuple[str, str]]:
    """(file, name) of the symbols that can be reached from another file of the same scope."""
    out = []
    for file, _, kind, name in symbols:
        if not file.endswith(SCOPE_SUFFIXES):
            continue
        if kind in TYPE_KINDS or (file.endswith(".go") and kind == "func" and name[:1].isupper()):
            out.append((file, name))
    return out


def scope_edges(project: Path, files: list[str], symbols: list[tuple[str, int, str, str]], read, deadline: float) -> dict[str, set[str]]:
    """Edges for Go, Java, Kotlin and C# files, found through shared scopes (see above)."""
    defined = _scope_symbols(symbols)
    if not defined:
        return {}
    modules: list[tuple[str, str]] = []  # Go: (folder of go.mod, module path)
    for f in files:
        if f.rsplit("/", 1)[-1] == "go.mod" and (text := read(project / f)) and (m := _GO_MODULE.search(text)):
            modules.append((posixpath.dirname(f), m.group(1)))
    modules.sort(key=lambda m: -len(m[0]))

    by_dir: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))  # Go/Java/Kotlin scope: a folder
    by_ns: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))  # C# scope: a namespace
    cs_namespaces: dict[str, list[str]] = {}
    for file, name in defined:
        if file.endswith(".cs"):
            if file not in cs_namespaces:
                text = read(project / file) or ""
                cs_namespaces[file] = _CS_NAMESPACE.findall(text)
            for ns in cs_namespaces[file]:
                by_ns[ns][name].add(file)
        else:
            by_dir[posixpath.dirname(file)][name].add(file)

    edges: dict[str, set[str]] = defaultdict(set)
    for rel in files:
        if time.monotonic() > deadline:
            break
        if not rel.endswith(SCOPE_SUFFIXES):
            continue
        text = read(project / rel)
        if text is None:
            continue
        idents = set(_IDENT.findall(text))
        scopes: list[dict[str, set[str]]] = []
        if rel.endswith(".cs"):
            own = _CS_NAMESPACE.findall(text)
            names = set(own) | set(_CS_USING.findall(text))
            for ns in own:  # `namespace A.B.C` also sees A.B and A
                parts = ns.split(".")
                names |= {".".join(parts[:i]) for i in range(1, len(parts))}
            scopes = [by_ns[n] for n in names if n in by_ns]
        else:
            scopes = [by_dir.get(posixpath.dirname(rel), {})]
            if rel.endswith(".go"):
                for block, single in _GO_IMPORT.findall(text):
                    for spec in (_QUOTED.findall(block) if block else [single]):
                        for folder, module in modules:
                            if rel.startswith(folder + "/") or not folder:
                                if spec == module or spec.startswith(module + "/"):
                                    pkg = posixpath.normpath(posixpath.join(folder, spec[len(module):].lstrip("/")))
                                    scopes.append(by_dir.get("" if pkg == "." else pkg, {}))
                                    break
        for scope in scopes:
            for name in idents & scope.keys():
                for target in scope[name]:
                    if target != rel:
                        edges[rel].add(target)
    return edges
