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
