from __future__ import annotations

import os

from llm_gateway.cli import load_dotenv


def test_load_dotenv_keeps_existing_variables(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nNEW_KEY="abc"\nexport OTHER_KEY=xyz\nEXISTING=from-file\n\n', encoding="utf-8")
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.delenv("OTHER_KEY", raising=False)
    monkeypatch.setenv("EXISTING", "from-shell")

    load_dotenv(env)

    assert os.environ["NEW_KEY"] == "abc"
    assert os.environ["OTHER_KEY"] == "xyz"
    assert os.environ["EXISTING"] == "from-shell"
    monkeypatch.delenv("NEW_KEY")
    monkeypatch.delenv("OTHER_KEY")


def test_load_dotenv_missing_file_is_ignored(tmp_path):
    load_dotenv(tmp_path / "nope.env")
