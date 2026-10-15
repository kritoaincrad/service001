"""Where things are: pages, server routes and top-level symbols, found with cheap pattern matching.

This feeds the project map and the project index. It answers "which file is the /orders page" or
"where is OrderForm defined" in one lookup instead of five searches. It is heuristic on purpose
(regular expressions, no parsers): a miss only costs Claude one normal search.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import code_graph
from .code_graph import RESOLVE_SUFFIXES
from .code_graph import resolve_js as _resolve

JS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
SCAN_SUFFIXES = JS | {".vue", ".svelte", ".py", ".php", ".java", ".kt", ".cs", ".razor", ".go", ".rs", ".rb"}
COMPONENT_SUFFIXES = {".tsx", ".jsx", ".vue", ".svelte", ".dart", ".cshtml", ".razor", ".astro"}
PAGE_DIRS = {"pages", "views", "screens", "routes"}
MAX_SCAN_FILES = 2500
MAX_BYTES = 300_000
MAX_SYMBOLS_PER_FILE = 40
SCAN_SYMBOLS_PER_FILE = 150  # collected per file, then trimmed to the most referenced
MAX_USES = 5
MAX_PACKAGES = 60
MAX_USES_PAGES = 150
SCAN_BUDGET_SECONDS = 8.0
ENTRY_NAMES = {
    "main.py", "__main__.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "server.py", "index.js", "index.ts",
    "index.tsx", "main.ts", "main.tsx", "main.js", "app.ts", "app.js", "server.js", "server.ts", "App.tsx", "App.jsx",
    "App.vue", "Program.cs", "Startup.cs", "main.go", "main.rs", "Application.java", "main.dart", "artisan",
    "app.module.ts", "app.routes.ts", "app-routing.module.ts",
}

_TEST = re.compile(r"(^|/)(tests?|__tests__|e2e|cypress|__mocks__)/|\.(test|spec)\.|(^|/)(test_[^/]*|tests|conftest)\.py$|_test\.(py|go)$|(Tests?|Spec)\.(cs|java|kt)$")
_ROUTER_HINT = re.compile(r"RouterModule|createRouter|createBrowserRouter|createHashRouter|useRoutes|VueRouter|<Routes")
_SERVER_HINT = re.compile(r"\b(?:express|fastify|koa|hono|restify)\b")
_CLIENT_RECEIVERS = {"axios", "http", "https", "client", "request", "supertest", "cy", "agent", "api", "fetch"}

_JSX_ROUTE = re.compile(r"<Route\b[^>]*?\bpath=\{?[\"'`]([^\"'`]*)[\"'`]")
_OBJ_PATH = re.compile(r"\bpath:\s*[\"'`]([^\"'`]*)[\"'`]")
_COMPONENT = re.compile(
    r"\b(?:element|Component|component|loadComponent|lazy)\s*[=:]\s*\{?\s*(?:\(\)\s*=>\s*)?<?\s*([A-Za-z_$][\w$]*)")
_INLINE_IMPORT = re.compile(r"\bimport\(\s*[\"'`]([^\"'`]+)[\"'`]")
_IMPORT_FROM = re.compile(r"\bimport\s+(?:type\s+)?([^;\"']*?)\s+from\s+[\"']([^\"']+)[\"']")
_LAZY_CONST = re.compile(
    r"\b(?:const|let|var)\s+([\w$]+)\s*=\s*(?:React\.)?lazy\(\s*\(\)\s*=>\s*import\(\s*[\"']([^\"']+)[\"']")
_EXPRESS = re.compile(r"\b(\w+)\.(get|post|put|patch|delete|all)\(\s*[\"'`](/[^\"'`]*)")
_NEST_CTRL = re.compile(r"@Controller\(\s*[\"'`]?([^\"'`)]*)")
_NEST_ACT = re.compile(r"@(Get|Post|Put|Patch|Delete|All)\(\s*[\"'`]?([^\"'`)]*)")
_PY_DECOR = re.compile(
    r"^\s*@[\w.]+\.(get|post|put|patch|delete|route|websocket|api_route|head|options)\(\s*[\"']([^\"']*)[\"']")
_PY_METHODS = re.compile(r"methods\s*=\s*[\[(]([^\])]*)")
_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")
_DJANGO = re.compile(r"\b(?:re_)?path\(\s*r?[\"']([^\"']*)[\"']\s*,\s*([\w.]+)")
_LARAVEL = re.compile(
    r"Route::(get|post|put|patch|delete|any|view|resource|match)\(\s*(?:\[[^\]]*\]\s*,\s*)?[\"']([^\"']*)[\"']")
_CLASS_REF = re.compile(r"(\w+)::class")
_SPRING = re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b(?:\(\s*(?:(?:value|path)\s*=\s*)?\{?\s*(?:\"([^\"]*)\")?)?")
_CS_ROUTE = re.compile(r"\[Route\(\s*\"([^\"]*)\"")
_CS_HTTP = re.compile(r"\[Http(Get|Post|Put|Patch|Delete)(?:\(\s*\"([^\"]*)\")?")
_CS_MIN = re.compile(r"\.Map(Get|Post|Put|Patch|Delete)\(\s*\"([^\"]*)\"")
_GO = re.compile(r"\.(GET|POST|PUT|PATCH|DELETE|HandleFunc|Handle)\(\s*\"([^\"]+)\"")
_RAZOR_PAGE = re.compile(r"^@page\s+\"([^\"]*)\"")
_RAILS_RESOURCES = re.compile(r"^\s*(resources?)\s+:(\w+)")
_RAILS_VERB = re.compile(
    r"^\s*(get|post|put|patch|delete|match)\s+['\"]?:?([^'\" ,]+)['\"]?(?:.*?(?:to:|=>)\s*['\"](\w+)#(\w+)['\"])?")
_RAILS_ROOT = re.compile(r"^\s*root\s+(?:to:\s*)?['\"](\w+)#(\w+)['\"]")
_CALL_NAME = re.compile(r"(\w+)\s*\(")

_JS_SYMBOLS = (
    re.compile(r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?(function\*?|class|interface|type|enum|abstract class)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^export\s+(?:default\s+)?(const|let|var)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^(const)\s+([A-Z][\w$]*)\s*="),
)
_OOP = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|abstract|sealed|static|partial|final|open|data|export)\s+)*"
    r"(class|interface|enum|record|struct|trait|object)\s+([A-Za-z_]\w*)")
_SYMBOL_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    ".py": (re.compile(r"^(class|async def|def)\s+([A-Za-z]\w*)"),),
    ".go": (re.compile(r"^(func)\s+(?:\([^)]*\)\s*)?([A-Za-z]\w*)"), re.compile(r"^(type)\s+([A-Za-z]\w*)\s+(?:struct|interface)")),
    ".rs": (re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(fn|struct|enum|trait)\s+([A-Za-z]\w*)"),),
    ".rb": (re.compile(r"^\s*(class|module)\s+([A-Z]\w*)"),),
    ".java": (_OOP,), ".kt": (_OOP,), ".cs": (_OOP,), ".php": (_OOP,),
    **{s: _JS_SYMBOLS for s in JS},
}


@dataclass
class Route:
    kind: str  # "page" | "api"
    path: str
    file: str
    line: int = 1
    method: str = ""
    name: str = ""
    via: str = ""  # where the route is declared, when that is not the page file itself
    uses: list[str] = field(default_factory=list)


@dataclass
class CodeIndex:
    routes: list[Route] = field(default_factory=list)
    symbols: list[tuple[str, int, str, str]] = field(default_factory=list)  # file, line, kind, name
    page_files: list[str] = field(default_factory=list)  # view/page components with no detected route
    entry_points: list[str] = field(default_factory=list)
    imports: dict[str, list[str]] = field(default_factory=dict)  # file -> project files it imports
    importers: dict[str, list[str]] = field(default_factory=dict)  # file -> project files that import it
    refs: dict[str, int] = field(default_factory=dict)  # symbol name -> number of other files that mention it


CONFIG_NAME = "cc-optimizer.json"


@dataclass
class ProjectConfig:
    """Optional per-project settings, .claude/cc-optimizer.json (safe to commit and share):

    {"ignore": ["legacy/**", "*.generated.ts"],
     "routes": [{"files": "src/**/*.ts", "regex": "registerPage\(\"(?P<path>[^\"]+)\"", "kind": "page"}]}

    A custom route regex needs a `path` group and may have `method` and `name` groups.
    """

    ignore: list[str] = field(default_factory=list)
    routes: list[tuple[str, re.Pattern, str]] = field(default_factory=list)  # file glob, regex, kind
    raw: str = ""  # the file text, so a change refreshes the map

    def ignored(self, rel: str) -> bool:
        return any(fnmatch.fnmatchcase(rel, g) or fnmatch.fnmatchcase(posixpath.basename(rel), g) for g in self.ignore)


def load_project_config(project: Path) -> ProjectConfig:
    try:
        raw = (project / ".claude" / CONFIG_NAME).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return ProjectConfig()
    if not isinstance(data, dict):
        return ProjectConfig()
    cfg = ProjectConfig(raw=raw)
    cfg.ignore = [g for g in data.get("ignore", []) if isinstance(g, str)] if isinstance(data.get("ignore"), list) else []
    for entry in data.get("routes", []) if isinstance(data.get("routes"), list) else []:
        try:
            regex = re.compile(entry["regex"])
            if "path" not in regex.groupindex:
                continue
            cfg.routes.append((str(entry.get("files", "*")), regex, "page" if entry.get("kind") == "page" else "api"))
        except (KeyError, TypeError, re.error):
            continue
    return cfg


def _custom_routes(rel: str, lines: list[str], cfg: ProjectConfig) -> list[Route]:
    out: list[Route] = []
    for glob, regex, kind in cfg.routes:
        if not (fnmatch.fnmatchcase(rel, glob) or fnmatch.fnmatchcase(posixpath.basename(rel), glob)):
            continue
        for i, line in enumerate(lines):
            if len(line) <= 400 and (m := regex.search(line)):
                groups = m.groupdict()
                out.append(Route(kind, groups["path"], rel, i + 1, (groups.get("method") or "").upper(),
                                 groups.get("name") or ""))
    return out


def is_test(rel: str) -> bool:
    return bool(_TEST.search(rel))


def _js_deps(project: Path, files: list[str]) -> dict[str, set[str]]:
    """Dependencies of every package.json, keyed by its folder, so each app of a monorepo is judged on its own."""
    deps: dict[str, set[str]] = {}
    for rel in [f for f in files if f.rsplit("/", 1)[-1] == "package.json" and "node_modules" not in f][:MAX_PACKAGES]:
        try:
            data = json.loads((project / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found: set[str] = set()
        for key in ("dependencies", "devDependencies"):
            if isinstance(data.get(key), dict):
                found |= set(data[key])
        deps[posixpath.dirname(rel)] = found
    return deps


def _deps_for(rel: str, deps: dict[str, set[str]]) -> set[str]:
    folder = posixpath.dirname(rel)
    while True:
        if folder in deps:
            return deps[folder]
        if not folder:
            return set()
        folder = posixpath.dirname(folder)


# ---- file-based routing -------------------------------------------------------------------------------------

def _url(raw: str) -> str:
    segments = []
    for seg in raw.split("/"):
        if not seg or seg.startswith(("(", "@")):
            continue
        seg = re.sub(r"\[\[\.\.\.(\w+)\]\]", r"*\1?", seg)
        seg = re.sub(r"\[\.\.\.(\w+)\]", r"*\1", seg)
        segments.append(re.sub(r"\[(\w+)\]", r":\1", seg))
    if segments and segments[-1] == "index":
        segments.pop()
    return "/" + "/".join(segments)


_NEXT_APP = re.compile(r"(?:^|/)app/((?:[^/]+/)*)(page|route)\.(?:tsx|jsx|ts|js|mjs)$")
_KIT = re.compile(r"(?:^|/)routes/((?:[^/]+/)*)\+(page|server)\.(?:svelte|ts|js)$")
_PAGES_DIR = re.compile(r"(?:^|/)pages/(.+)\.(?:tsx|jsx|ts|js|mjs|vue|svelte|astro|mdx|md)$")
_NUXT_SERVER = re.compile(r"(?:^|/)server/(?:api|routes)/(.+)\.(?:ts|js)$")
_RAZOR_PAGES = re.compile(r"(?:^|/)Pages/(.+)\.cshtml$")
_MVC_VIEW = re.compile(r"(?:^|/)Views/([^/]+)/([^/]+)\.cshtml$")


def _path_routes(rel: str, deps: set[str]) -> list[Route]:
    if m := _RAZOR_PAGES.search(rel):
        name = m.group(1)
        if name.startswith("Shared/") or name.startswith("_") or "/_" in name:
            return []
        return [Route("page", _url(name), rel)]
    if m := _MVC_VIEW.search(rel):
        ctrl, action = m.groups()
        if ctrl == "Shared" or action.startswith("_"):
            return []
        return [Route("page", "/" if (ctrl, action) == ("Home", "Index") else f"/{ctrl}/{action}", rel)]
    if "next" in deps and (m := _NEXT_APP.search(rel)):
        return [Route("api" if m.group(2) == "route" else "page", _url(m.group(1)), rel)]
    if "@sveltejs/kit" in deps and (m := _KIT.search(rel)):
        return [Route("api" if m.group(2) == "server" else "page", _url(m.group(1)), rel)]
    if {"nuxt", "nuxt3"} & deps and (m := _NUXT_SERVER.search(rel)):
        return [Route("api", "/api/" + _url(m.group(1)).lstrip("/"), rel)]
    if {"next", "nuxt", "nuxt3", "astro"} & deps and (m := _PAGES_DIR.search(rel)):
        name = m.group(1)
        if any(part.startswith("_") for part in name.split("/")):
            return []
        if name.startswith("api/"):
            return [Route("api", _url(name), rel)]
        return [Route("page", _url(name), rel)]
    return []


# ---- imports and component resolution -----------------------------------------------------------------------

def _import_map(rel: str, text: str, fileset: set[str]) -> dict[str, str]:
    """Local name -> project file, for the imports of one JS/TS/Vue/Svelte file."""
    out: dict[str, str] = {}
    for clause, spec in _IMPORT_FROM.findall(text):
        target = _resolve(spec, rel, fileset)
        if not target:
            continue
        head, _, rest = clause.partition("{")
        default = head.strip().rstrip(",").strip()
        if default and not default.startswith("*"):
            out[default] = target
        elif default.startswith("* as "):
            out[default[5:].strip()] = target
        for part in rest.split("}")[0].split(","):
            part = part.strip()
            if part:
                out[part.split(" as ")[-1].strip()] = target
    for name, spec in _LAZY_CONST.findall(text):
        target = _resolve(spec, rel, fileset)
        if target:
            out[name] = target
    return out


def _window(lines: list[str], i: int, stop: str) -> str:
    joined = lines[i]
    for nxt in lines[i + 1:i + 5]:
        if stop in nxt:
            break
        joined += " " + nxt
    return joined


def _next_call(lines: list[str], i: int) -> str:
    """Name of the function declared right after a decorator / attribute line."""
    for nxt in lines[i + 1:i + 4]:
        stripped = nxt.strip()
        if not stripped or stripped.startswith(("@", "[", "//", "#")):
            continue
        m = _PY_DEF.match(nxt) or _CALL_NAME.search(nxt)
        return m.group(1) if m else ""
    return ""


# ---- content-based routes -----------------------------------------------------------------------------------

def _js_routes(rel: str, text: str, lines: list[str], fileset: set[str]) -> list[Route]:
    imports = _import_map(rel, text, fileset)
    is_router = "rout" in rel.lower() or bool(_ROUTER_HINT.search(text))
    server = bool(_SERVER_HINT.search(text))
    nest = "@nestjs" in text
    out: list[Route] = []
    prefix = ""
    for i, line in enumerate(lines):
        if len(line) > 400:
            continue
        jsx = _JSX_ROUTE.search(line)
        obj = None if jsx or not is_router else _OBJ_PATH.search(line)
        if jsx or obj:
            m = jsx or obj
            window = _window(lines, i, "<Route" if jsx else "path:")
            comp = _COMPONENT.search(window)
            name = comp.group(1) if comp and comp.group(1) not in ("lazy", "import") else ""
            inline = _INLINE_IMPORT.search(window)
            target = (_resolve(inline.group(1), rel, fileset) if inline else None) or imports.get(name)
            if not (jsx or name or inline):
                continue
            path = m.group(1) or "/"
            if "redirectTo" in window and not (name or inline):
                continue
            out.append(Route("page", path, target or rel, 1 if target else i + 1, name=name,
                             via=f"{rel}:{i + 1}" if target else ""))
            continue
        if nest:
            if m := _NEST_CTRL.search(line):
                prefix = m.group(1).strip("/")
            elif m := _NEST_ACT.search(line):
                path = "/" + "/".join(p for p in (prefix, m.group(2).strip("/")) if p)
                out.append(Route("api", path, rel, i + 1, m.group(1).upper(), _next_call(lines, i)))
        if server and (m := _EXPRESS.search(line)) and m.group(1) not in _CLIENT_RECEIVERS:
            out.append(Route("api", m.group(3), rel, i + 1, m.group(2).upper()))
    return out


def _py_routes(rel: str, text: str, lines: list[str]) -> list[Route]:
    out: list[Route] = []
    django = "urlpatterns" in text
    for i, line in enumerate(lines):
        if len(line) > 400:
            continue
        if m := _PY_DECOR.match(line):
            verb, path = m.groups()
            if verb in ("route", "api_route"):
                methods = _PY_METHODS.search(line)
                verb = re.sub(r"[\s\"']", "", methods.group(1)).upper() if methods else "GET"
            out.append(Route("api", path or "/", rel, i + 1, verb.upper(), _next_call(lines, i)))
        elif django and (m := _DJANGO.search(line)):
            path = m.group(1)
            out.append(Route("api", path if path.startswith("/") else "/" + path, rel, i + 1, "", m.group(2)))
    return out


def _jvm_routes(rel: str, lines: list[str]) -> list[Route]:
    out: list[Route] = []
    prefix, seen_action = "", False
    for i, line in enumerate(lines):
        if len(line) > 400:
            continue
        m = _SPRING.search(line)
        if not m:
            continue
        verb, path = m.group(1), m.group(2) or ""
        if verb == "Request" and not seen_action:
            prefix = path.strip("/")
            continue
        seen_action = True
        full = "/" + "/".join(p for p in (prefix, path.strip("/")) if p)
        out.append(Route("api", full, rel, i + 1, "ANY" if verb == "Request" else verb.upper(), _next_call(lines, i)))
    return out


def _cs_routes(rel: str, lines: list[str]) -> list[Route]:
    out: list[Route] = []
    stem = posixpath.basename(rel).rsplit(".", 1)[0].removesuffix("Controller")
    prefix, seen_action = "", False
    for i, line in enumerate(lines):
        if len(line) > 400:
            continue
        if (m := _CS_ROUTE.search(line)) and not seen_action:
            prefix = m.group(1).replace("[controller]", stem).strip("/")
        elif m := _CS_HTTP.search(line):
            seen_action = True
            path = "/" + "/".join(p for p in (prefix, (m.group(2) or "").strip("/")) if p)
            out.append(Route("api", path, rel, i + 1, m.group(1).upper(), _next_call(lines, i)))
        elif m := _CS_MIN.search(line):
            out.append(Route("api", m.group(2) or "/", rel, i + 1, m.group(1).upper()))
    return out


def _simple_routes(rel: str, suffix: str, lines: list[str]) -> list[Route]:
    out: list[Route] = []
    for i, line in enumerate(lines):
        if len(line) > 400:
            continue
        if suffix == ".php" and (m := _LARAVEL.search(line)):
            ref = _CLASS_REF.search(line[m.end():])
            path = m.group(2)
            out.append(Route("api", path if path.startswith("/") else "/" + path, rel, i + 1, m.group(1).upper(),
                             ref.group(1) if ref else ""))
        elif suffix == ".go" and (m := _GO.search(line)):
            verb, path = m.groups()
            if " " in path:  # Go 1.22 patterns: "GET /orders"
                verb, _, path = path.partition(" ")
            elif verb in ("HandleFunc", "Handle"):
                verb = ""
            out.append(Route("api", path, rel, i + 1, verb.upper()))
        elif suffix == ".razor" and (m := _RAZOR_PAGE.match(line)):
            out.append(Route("page", m.group(1) or "/", rel, i + 1))
    return out


def _rails_routes(rel: str, lines: list[str], fileset: set[str]) -> list[Route]:
    """config/routes.rb. Namespaces and scopes are not joined to the paths."""
    def controller(name: str) -> str:
        cand = f"app/controllers/{name}_controller.rb"
        return cand if cand in fileset else rel

    out: list[Route] = []
    for i, line in enumerate(lines):
        if m := _RAILS_RESOURCES.match(line):
            out.append(Route("api", "/" + m.group(2), controller(m.group(2)), 1, m.group(1).upper(), f"routes.rb:{i + 1}"))
        elif m := _RAILS_ROOT.match(line):
            out.append(Route("api", "/", controller(m.group(1)), 1, "GET", m.group(2)))
        elif m := _RAILS_VERB.match(line):
            verb, path, ctrl, action = m.groups()
            out.append(Route("api", path if path.startswith("/") else "/" + path, controller(ctrl) if ctrl else rel,
                             1 if ctrl else i + 1, verb.upper(), action or ""))
    return out


def _content_routes(rel: str, suffix: str, text: str, lines: list[str], fileset: set[str]) -> list[Route]:
    if suffix in JS:
        return _js_routes(rel, text, lines, fileset)
    if suffix == ".py":
        return _py_routes(rel, text, lines)
    if suffix in (".java", ".kt"):
        return _jvm_routes(rel, lines)
    if suffix == ".cs":
        return _cs_routes(rel, lines)
    if suffix == ".rb" and (rel.endswith("config/routes.rb") or "config/routes/" in rel):
        return _rails_routes(rel, lines, fileset)
    return _simple_routes(rel, suffix, lines)


# ---- symbols ------------------------------------------------------------------------------------------------

def _symbols(rel: str, suffix: str, lines: list[str]) -> list[tuple[str, int, str, str]]:
    patterns = _SYMBOL_PATTERNS.get(suffix)
    if not patterns:
        return []
    out: list[tuple[str, int, str, str]] = []
    for i, line in enumerate(lines, 1):
        if len(line) > 300:
            continue
        for pat in patterns:
            if m := pat.match(line):
                kind = {"async def": "def", "abstract class": "class"}.get(m.group(1), m.group(1))
                if suffix == ".go" and kind == "func" and line.startswith("func ("):
                    kind = "method"  # reachable only through its type, not a name other files can mention
                out.append((rel, i, kind, m.group(2)))
                break
        if len(out) >= SCAN_SYMBOLS_PER_FILE:
            break
    return out


# ---- scan ---------------------------------------------------------------------------------------------------

def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if ".min." in path.name or len(text) > 300 * (text.count("\n") + 1):
        return None  # minified or generated: nothing readable to index
    return text


def _unique(routes: list[Route]) -> list[Route]:
    seen: set[tuple] = set()
    out: list[Route] = []
    for r in routes:
        key = (r.kind, r.method, r.path, r.file)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _rank_symbols(project: Path, index: CodeIndex, files: list[str], deadline: float) -> None:
    """Count how many files mention each defined symbol, and keep the most referenced ones of big files."""
    definitions: dict[str, set[str]] = defaultdict(set)
    for file, _, _, name in index.symbols:
        definitions[name].add(file)
    refs = code_graph.count_references(project, files, definitions, _read, deadline)
    index.refs = {name: n for name, n in refs.items() if n}
    by_file: dict[str, list[tuple[str, int, str, str]]] = defaultdict(list)
    for sym in index.symbols:
        by_file[sym[0]].append(sym)
    kept: list[tuple[str, int, str, str]] = []
    for syms in by_file.values():
        if len(syms) > MAX_SYMBOLS_PER_FILE:
            syms = sorted(sorted(syms, key=lambda x: -index.refs.get(x[3], 0))[:MAX_SYMBOLS_PER_FILE], key=lambda x: x[1])
        kept += syms
    index.symbols = kept


def scan(project: Path, files: list[str], cfg: ProjectConfig | None = None) -> CodeIndex:
    cfg = cfg or ProjectConfig()
    fileset = code_graph.FileIndex(files, project)
    deps = _js_deps(project, files)
    index = CodeIndex()
    started = time.monotonic()
    resolver = code_graph.Resolver(fileset)
    read_files: list[str] = []
    scanned = 0
    for rel in files:
        if is_test(rel):
            continue
        index.routes += _path_routes(rel, _deps_for(rel, deps))
        suffix = Path(rel).suffix.lower()
        if (suffix not in SCAN_SUFFIXES and not cfg.routes) or scanned >= MAX_SCAN_FILES or time.monotonic() - started > SCAN_BUDGET_SECONDS:
            continue
        text = _read(project / rel)
        if text is None:
            continue
        scanned += 1
        read_files.append(rel)
        lines = text.splitlines()
        if targets := code_graph.imports_of(rel, suffix, text, resolver):
            index.imports[rel] = targets
        index.symbols += _symbols(rel, suffix, lines)
        index.routes += _content_routes(rel, suffix, text, lines, fileset)
        if cfg.routes:
            index.routes += _custom_routes(rel, lines, cfg)
    index.routes = _unique(index.routes)
    for src, targets in code_graph.scope_edges(project, files, index.symbols, _read, started + SCAN_BUDGET_SECONDS * 1.5).items():
        index.imports[src] = sorted(set(index.imports.get(src, [])) | targets)
    index.importers = code_graph.reverse(index.imports)
    _rank_symbols(project, index, read_files, started + SCAN_BUDGET_SECONDS * 1.5)

    pages = [r for r in index.routes if r.kind == "page"]
    for route in pages[:MAX_USES_PAGES]:
        if Path(route.file).suffix.lower() not in RESOLVE_SUFFIXES:
            continue
        text = _read(project / route.file)
        if text is None:
            continue
        names = []
        for target in dict.fromkeys(_import_map(route.file, text, fileset).values()):
            if Path(target).suffix.lower() in RESOLVE_SUFFIXES and target != route.file:
                stem = posixpath.basename(target).rsplit(".", 1)[0]
                names.append(posixpath.basename(posixpath.dirname(target)) or stem if stem == "index" else stem)
        route.uses = names[:MAX_USES]

    route_files = {r.file for r in pages}
    index.page_files = [
        f for f in files
        if Path(f).suffix.lower() in COMPONENT_SUFFIXES and f not in route_files and not is_test(f)
        and PAGE_DIRS & set(f.split("/")[:-1])
    ]
    index.entry_points = [
        f for f in files if f.count("/") <= 3 and f.rsplit("/", 1)[-1] in ENTRY_NAMES and not is_test(f)
    ]
    return index
