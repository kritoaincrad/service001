from __future__ import annotations

import json
import os

import pytest

from cc_optimizer import hooks, project_map
from cc_optimizer.guard_config import DEFAULTS


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path / "home"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    (root / "src" / "api").mkdir(parents=True)
    (root / "node_modules" / "lib").mkdir(parents=True)
    (root / "README.md").write_text("# Shop\n\nOnline shop with an API and a web client.\nSecond line.\n\n## More\n", encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"name": "shop", "scripts": {"dev": "vite", "test": "vitest"}}), encoding="utf-8")
    (root / "src" / "api" / "orders.ts").write_text("/** Order endpoints: create, list, cancel. */\nexport {}\n", encoding="utf-8")
    (root / "src" / "main.py").write_text('"""Entry point of the worker."""\nimport os\n', encoding="utf-8")
    (root / "src" / "plain.py").write_text("x = 1\n", encoding="utf-8")
    (root / "node_modules" / "lib" / "index.js").write_text("// vendored\n", encoding="utf-8")
    return root


def test_is_project(tmp_path, project, monkeypatch):
    assert project_map.is_project(project)
    (tmp_path / "loose").mkdir()
    assert not project_map.is_project(tmp_path / "loose")
    monkeypatch.setattr(project_map.Path, "home", lambda: project)
    assert not project_map.is_project(project)  # the home folder is never a project


def test_build_map_content(project):
    text = project_map.build_map(project)
    assert "Online shop with an API and a web client. Second line." in text
    assert "- package.json (shop) scripts: dev: vite, test: vitest" in text
    assert "orders.ts - Order endpoints: create, list, cancel." in text
    assert "main.py - Entry point of the worker." in text
    assert "plain.py\n" in text
    assert "node_modules" not in text and "index.js" not in text


def test_ensure_map_refreshes_on_new_commit_and_excludes_from_git(project):
    (project / ".git" / "refs" / "heads").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / ".git" / "refs" / "heads" / "main").write_text("aaa111\n", encoding="utf-8")

    path = project_map.ensure_map(project)
    assert path == project / ".claude" / "project-map.md" and "head=aaa111" in path.read_text(encoding="utf-8")
    old = path.stat().st_mtime - 100
    os.utime(path, (old, old))
    project_map.ensure_map(project)
    assert path.stat().st_mtime == old  # fresh: not rebuilt

    (project / ".git" / "refs" / "heads" / "main").write_text("bbb222\n", encoding="utf-8")
    project_map.ensure_map(project)
    assert "head=bbb222" in path.read_text(encoding="utf-8")
    project_map.ensure_map(project, force=True)
    exclude = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.splitlines().count(".claude/project-map.md") == 1


def test_session_start_points_to_the_map(project):
    out = hooks.session_start({"cwd": str(project)}, {**DEFAULTS, "model_routing": False})
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "project-map.md" in context and "Read it before exploring" in context
    assert "Online shop" not in context  # only the path enters the context
    off = hooks.session_start({"cwd": str(project)}, {**DEFAULTS, "project_map": False, "model_routing": False})
    assert "project-map" not in off["hookSpecificOutput"]["additionalContext"]


def test_large_projects_are_summarized(tmp_path):
    root = tmp_path / "big"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'big'\n", encoding="utf-8")
    for i in range(400):
        (root / "src" / f"m{i}.py").write_text('"""Module."""\n', encoding="utf-8")
    text = project_map.build_map(root)
    assert "src/ 400 files (.py 400 here)" in text
    assert len(text.splitlines()) < 30


@pytest.fixture
def webapp(tmp_path):
    root = tmp_path / "webapp"
    for d in ("src/pages", "src/components", "api", "app/(shop)/cart", "app/api/orders"):
        (root / d).mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "w", "dependencies": {"react-router-dom": "6"}}), encoding="utf-8")
    (root / "src/App.tsx").write_text(
        "import Orders from './pages/Orders'\nimport { Home } from './pages/Home'\n"
        "const Lazy = lazy(() => import('./pages/Lazy'))\n"
        "export default function App() {\n  return <Routes>\n"
        "    <Route path=\"/\" element={<Home />} />\n"
        "    <Route path=\"/orders/:id\" element={<Orders />} />\n"
        "    <Route path=\"/lazy\" element={<Lazy />} />\n  </Routes>\n}\n", encoding="utf-8")
    (root / "src/pages/Orders.tsx").write_text("import OrderForm from '../components/OrderForm'\nexport default function Orders() {}\n", encoding="utf-8")
    (root / "src/pages/Home.tsx").write_text("export function Home() {}\n", encoding="utf-8")
    (root / "src/pages/Lazy.tsx").write_text("export default function Lazy() {}\n", encoding="utf-8")
    (root / "src/pages/Orphan.tsx").write_text("export default function Orphan() {}\n", encoding="utf-8")
    (root / "src/components/OrderForm.tsx").write_text("export const OrderForm = () => null\n", encoding="utf-8")
    (root / "api/server.js").write_text(
        "const express = require('express')\nconst app = express()\n"
        "app.get('/api/orders', list)\napp.post('/api/orders', create)\naxios.get('/not-a-route')\n", encoding="utf-8")
    (root / "api/views.py").write_text(
        "@app.route('/health', methods=['GET', 'POST'])\ndef health():\n    pass\n\n"
        "@router.get('/items/{id}')\nasync def item():\n    pass\n", encoding="utf-8")
    return root


def test_pages_resolve_to_component_files(webapp):
    text = project_map.build_map(webapp)
    assert "/orders/:id  src/pages/Orders.tsx (Orders) <- route declared in src/App.tsx:7 | uses: OrderForm" in text
    assert "/  src/pages/Home.tsx (Home)" in text
    assert "/lazy  src/pages/Lazy.tsx (Lazy)" in text
    assert "src/pages/Orphan.tsx" in text.split("no detected route")[1]  # not routed, still listed


def test_server_routes_are_grouped_by_file(webapp):
    text = project_map.build_map(webapp)
    assert "api/server.js: GET /api/orders, POST /api/orders" in text
    assert "not-a-route" not in text  # axios client call, not a route
    assert "api/views.py: GET,POST /health (health), GET /items/{id} (item)" in text


def test_file_based_routing(tmp_path):
    root = tmp_path / "next"
    (root / "app/(shop)/cart").mkdir(parents=True)
    (root / "app/api/orders").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"dependencies": {"next": "14"}}), encoding="utf-8")
    (root / "app/(shop)/cart/page.tsx").write_text("export default function Cart() {}\n", encoding="utf-8")
    (root / "app/api/orders/route.ts").write_text("export async function GET() {}\n", encoding="utf-8")
    text = project_map.build_map(root)
    assert "/cart  app/(shop)/cart/page.tsx" in text
    assert "/api/orders" in text


def test_index_is_greppable_and_written_with_the_map(webapp):
    path = project_map.ensure_map(webapp)
    index = (path.parent / "project-index.md").read_text(encoding="utf-8")
    assert "OrderForm  src/components/OrderForm.tsx:1  const" in index
    assert "/orders/:id  src/pages/Orders.tsx  page  (Orders)" in index
    assert "health  api/views.py:2  def" in index
    exclude = (webapp / ".git" / "info" / "exclude")
    assert not exclude.exists()  # not a git repository here


def test_new_file_refreshes_the_map(webapp):
    path = project_map.ensure_map(webapp)
    old = path.stat().st_mtime - 100
    os.utime(path, (old, old))
    project_map.ensure_map(webapp)
    assert path.stat().st_mtime == old
    (webapp / "src/pages/New.tsx").write_text("export default function New() {}\n", encoding="utf-8")
    project_map.ensure_map(webapp)
    assert "New.tsx" in path.read_text(encoding="utf-8")


def test_monorepo_apps_are_judged_by_their_own_package_json(tmp_path):
    root = tmp_path / "mono"
    (root / "apps/web/pages").mkdir(parents=True)
    (root / "apps/docs/pages").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "root"}), encoding="utf-8")
    (root / "apps/web/package.json").write_text(json.dumps({"dependencies": {"next": "14"}}), encoding="utf-8")
    (root / "apps/docs/package.json").write_text(json.dumps({"dependencies": {"react": "18"}}), encoding="utf-8")
    (root / "apps/web/pages/about.tsx").write_text("export default function About() {}\n", encoding="utf-8")
    (root / "apps/docs/pages/Intro.tsx").write_text("export default function Intro() {}\n", encoding="utf-8")
    text = project_map.build_map(root)
    assert "/about  apps/web/pages/about.tsx" in text
    assert "\n/Intro  " not in text and "apps/docs/pages/Intro.tsx" in text  # no Next there: listed as a view file only


def test_tests_py_is_not_indexed(tmp_path):
    root = tmp_path / "dj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='dj'\n", encoding="utf-8")
    (root / "tests.py").write_text("@app.get('/x')\ndef test_x():\n    pass\n", encoding="utf-8")
    assert "/x" not in project_map.build_map(root)


def test_rails_routes(tmp_path):
    root = tmp_path / "rails"
    (root / "config").mkdir(parents=True)
    (root / "app/controllers").mkdir(parents=True)
    (root / "Gemfile").write_text("source 'x'\n", encoding="utf-8")
    (root / "app/controllers/articles_controller.rb").write_text("class ArticlesController\nend\n", encoding="utf-8")
    (root / "config/routes.rb").write_text(
        "Rails.application.routes.draw do\n  root 'articles#index'\n  resources :articles\n"
        "  get '/about', to: 'pages#about'\nend\n", encoding="utf-8")
    text = project_map.build_map(root)
    assert "app/controllers/articles_controller.rb: GET / (index), RESOURCES /articles (routes.rb:3)" in text
    assert "GET /about (about)" in text


def test_project_settings_ignore_and_custom_routes(webapp):
    (webapp / ".claude").mkdir()
    (webapp / "src/pages/Legacy.tsx").write_text("export default function Legacy() {}\n", encoding="utf-8")
    (webapp / "src/cms.ts").write_text("registerPage('/promo', 'PromoPage')\nbad = 1\n", encoding="utf-8")
    cfg = webapp / ".claude" / "cc-optimizer.json"
    path = project_map.ensure_map(webapp)
    assert "Legacy.tsx" in path.read_text(encoding="utf-8")
    cfg.write_text(json.dumps({
        "ignore": ["src/pages/Legacy.tsx", "Orphan.tsx"],
        "routes": [{"files": "src/*.ts", "kind": "page",
                    "regex": r"registerPage\('(?P<path>[^']+)', '(?P<name>\w+)'"}, {"regex": "(", "files": "*"}],
    }), encoding="utf-8")
    project_map.ensure_map(webapp)  # the settings file changed: map is rebuilt
    text = path.read_text(encoding="utf-8")
    assert "Legacy.tsx" not in text and "Orphan.tsx" not in text
    assert "/promo  src/cms.ts (PromoPage)" in text
