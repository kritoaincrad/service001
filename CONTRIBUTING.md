# Contributing

Thanks for your interest in Service001. Bug reports, ideas and pull requests are welcome.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
# .venv/bin/python -m pip install -e ".[dev]"     # Linux / macOS
```

## Before you open a pull request

```bash
python -m pytest -q      # all tests must pass
ruff check .             # no lint errors
```

- Add or update a test for every behavior change. Tests build their own fake session records, they never read your real `~/.claude`.
- Every user-facing message is written in English and Turkish next to each other (see `src/cc_optimizer/i18n.py`). Text that goes to Claude is English only.
- The hooks run on every tool call, so keep them fast and keep the package free of required dependencies. A hook must never break Claude Code: on an error it should disable itself silently.
- Do not add content from sessions (prompts, file contents, paths of real projects) to tests, docs or issues. Use made-up names.

## Reporting a bug

Please include the output of `cc-optimizer --version` and `cc-optimizer doctor`, your OS and Python version, and what you expected to happen. Claude Code's session record format is internal and changes between versions, so the Claude Code version helps too.
