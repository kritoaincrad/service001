from __future__ import annotations

import json

import pytest

from cc_optimizer import cli, code_graph, project_map


def write(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def jsapp(tmp_path):
    return write(tmp_path / "app", {
        "package.json": json.dumps({"name": "app"}),
        "tsconfig.json": '{\n // comments are allowed\n "compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["src/lib/*"]},},\n}',
        "src/lib/api.ts": "export function fetchOrders() {}\nexport const API_URL = 'x'\n",
        "src/lib/format.ts": "export function formatPrice() {}\n",
        "src/pages/Orders.tsx": "import { fetchOrders } from '@lib/api'\nimport { formatPrice } from '../lib/format'\nfetchOrders(); formatPrice()\n",
        "src/pages/Cart.tsx": "import { fetchOrders } from 'src/lib/api'\nexport * from './Orders'\nfetchOrders()\n",
        "src/main.ts": "const x = require('./lib/api')\nconst y = import('./pages/Cart')\n",
    })


def test_js_imports_resolve_aliases_and_forms(jsapp):
    idx = project_map.scan(jsapp, project_map.list_files(jsapp))
    assert idx.imports["src/pages/Orders.tsx"] == ["src/lib/api.ts", "src/lib/format.ts"]  # tsconfig paths + relative
    assert idx.imports["src/pages/Cart.tsx"] == ["src/lib/api.ts", "src/pages/Orders.tsx"]  # baseUrl style + export-from
    assert idx.imports["src/main.ts"] == ["src/lib/api.ts", "src/pages/Cart.tsx"]  # require() and import()
    assert idx.importers["src/lib/api.ts"] == ["src/main.ts", "src/pages/Cart.tsx", "src/pages/Orders.tsx"]


def test_symbol_references_are_counted_per_file(jsapp):
    idx = project_map.scan(jsapp, project_map.list_files(jsapp))
    assert idx.refs["fetchOrders"] == 2  # Orders.tsx and Cart.tsx, not the defining file
    assert idx.refs["formatPrice"] == 1


def test_map_and_index_show_hubs_and_dependencies(jsapp):
    text, index = project_map.build(jsapp)
    assert "src/lib/api.ts  imported by 3" in text
    assert "fetchOrders  src/lib/api.ts:1  in 2 files" in text
    assert "API_URL" not in text.split("## Hubs")[1].split("## Layout")[0]  # constants are not hubs
    assert "src/lib/api.ts  src/lib/api.ts  deps  (used by 3: main.ts, Cart.tsx, Orders.tsx)" in index
    assert "fetchOrders  src/lib/api.ts:1  function  (refs 2)" in index


def test_impact_follows_importers_transitively(jsapp):
    idx = project_map.scan(jsapp, project_map.list_files(jsapp))
    hit = dict(code_graph.impacted(idx.importers, "src/pages/Orders.tsx", depth=3))
    assert hit == {"src/pages/Cart.tsx": 1, "src/main.ts": 2}
    assert code_graph.impacted(idx.importers, "src/main.ts") == []


def test_python_imports(tmp_path):
    root = write(tmp_path / "py", {
        "pyproject.toml": "[project]\nname='x'\n",
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "import os\nimport json\nfrom .util import helper\nfrom . import models\n",
        "src/pkg/util.py": "def helper(): pass\n",
        "src/pkg/models.py": "class Order: pass\n",
        "src/pkg/cli.py": "from pkg.core import run\nimport pkg.util\nfrom pkg import models\n",
        "src/pkg/json.py": "x = 1\n",  # shadows a stdlib name: `import json` must not link to it
    })
    idx = project_map.scan(root, project_map.list_files(root))
    assert idx.imports["src/pkg/core.py"] == ["src/pkg/models.py", "src/pkg/util.py"]
    assert idx.imports["src/pkg/cli.py"] == ["src/pkg/core.py", "src/pkg/models.py", "src/pkg/util.py"]


def test_java_imports_and_ambiguous_names(tmp_path):
    root = write(tmp_path / "jv", {
        "pom.xml": "<project/>",
        "src/main/java/com/shop/model/Order.java": "package com.shop.model;\npublic class Order {}\n",
        "src/main/java/com/shop/web/OrderController.java": "package com.shop.web;\nimport com.shop.model.Order;\nimport java.util.List;\nclass OrderController {}\n",
        "src/main/java/com/a/Util.java": "package com.a;\nclass Util {}\n",
        "src/main/java/com/b/Util.java": "package com.b;\nclass Util {}\n",
    })
    idx = project_map.scan(root, project_map.list_files(root))
    assert idx.imports["src/main/java/com/shop/web/OrderController.java"] == ["src/main/java/com/shop/model/Order.java"]
    assert code_graph.Resolver(project_map.list_files(root)).module_file("Util") is None  # ambiguous: not guessed


def test_graph_json_is_plain_data(jsapp):
    data = project_map.graph_data(jsapp)
    api = next(f for f in data["files"] if f["path"] == "src/lib/api.ts")
    assert api["used_by"] == ["src/main.ts", "src/pages/Cart.tsx", "src/pages/Orders.tsx"]
    assert {"name": "fetchOrders", "line": 1, "kind": "function", "refs": 2} in api["symbols"]
    json.dumps(data)  # serializable


def test_impact_command(jsapp, capsys, monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_LANG", "en")
    from cc_optimizer import i18n

    i18n.reset()
    assert cli.main(["impact", "Orders.tsx", "--project-dir", str(jsapp)]) == 0
    out = capsys.readouterr().out
    assert "imported by 1" in out and "1  src/pages/Cart.tsx" in out and "2  src/main.ts" in out
    assert cli.main(["impact", "nope.ts", "--project-dir", str(jsapp)]) == 1


def test_csharp_edges_follow_namespaces_and_type_mentions(tmp_path):
    root = write(tmp_path / "cs", {
        "App.sln": "",
        "src/Core/Order.cs": "namespace Shop.Core.Models;\npublic class Order {}\npublic class OrderLine {}\n",
        "src/Core/Money.cs": "namespace Shop.Core;\npublic record Money(decimal Amount);\n",
        "src/Web/OrderController.cs": "using Shop.Core.Models;\nnamespace Shop.Web;\nclass OrderController { Order o; Money m; }\n",
        "src/Web/Other.cs": "using Shop.Core.Models;\nnamespace Shop.Web;\nclass Other { int x; }\n",
        "src/Core/Inside.cs": "namespace Shop.Core.Models;\nclass Inside { OrderLine l; Money m; }\n",
    })
    idx = project_map.scan(root, project_map.list_files(root))
    # a using brings the namespace in; only mentioned types make an edge. Money (Shop.Core) is not in a used namespace.
    assert idx.imports["src/Web/OrderController.cs"] == ["src/Core/Order.cs"]
    assert "src/Web/Other.cs" not in idx.imports  # imports a namespace but mentions none of its types
    assert idx.imports["src/Core/Inside.cs"] == ["src/Core/Money.cs", "src/Core/Order.cs"]  # same + parent namespace


def test_go_edges_use_module_imports_and_same_package(tmp_path):
    root = write(tmp_path / "go", {
        "go.mod": "module example.com/shop\n\ngo 1.22\n",
        "cmd/main.go": 'package main\nimport (\n\t"fmt"\n\t"example.com/shop/store"\n)\nfunc main() { store.NewStore(); fmt.Println() }\n',
        "store/store.go": "package store\ntype Store struct{}\nfunc NewStore() *Store { return nil }\nfunc (s *Store) Close() {}\n",
        "store/util.go": "package store\nfunc helper() { var s Store; _ = s }\n",
        "other/other.go": "package other\nfunc Close() {}\n",
    })
    idx = project_map.scan(root, project_map.list_files(root))
    assert idx.imports["cmd/main.go"] == ["store/store.go"]  # NewStore, found through the imported package
    assert idx.imports["store/util.go"] == ["store/store.go"]  # same package, no import needed
    assert "other/other.go" not in idx.importers  # nothing mentions its exported names
    kinds = {(file, name): kind for file, _, kind, name in idx.symbols}
    assert kinds[("store/store.go", "Close")] == "method"  # a method is not a name other files can mention


def test_java_same_package_edges(tmp_path):
    root = write(tmp_path / "jv", {
        "pom.xml": "<project/>",
        "src/com/shop/Order.java": "package com.shop;\npublic class Order {}\n",
        "src/com/shop/OrderService.java": "package com.shop;\nclass OrderService { Order find() { return null; } }\n",
        "src/com/shop/other/Unrelated.java": "package com.shop.other;\nclass Unrelated { int Order; }\n",
    })
    idx = project_map.scan(root, project_map.list_files(root))
    assert idx.imports["src/com/shop/OrderService.java"] == ["src/com/shop/Order.java"]  # no import statement needed
    assert "src/com/shop/other/Unrelated.java" not in idx.imports  # another package, none imported
