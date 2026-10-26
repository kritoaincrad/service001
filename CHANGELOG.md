# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-26

First packaged release.

### Added
- `cc-optimizer analyze`: token analysis of Claude Code sessions, with calibration against actual context growth and a compaction scenario.
- Context guards through Claude Code hooks: session rules, context-length warning, session-age warning, large-read and file-dump refusal, full-rewrite warning, status line.
- `install` / `uninstall` with backup and restore, `doctor` health check, `events` log of what the guards did.
- Project map and search index (`map`), import graph and `impact` command.
- Per-project work log that survives new chats and compaction.
- Permanent token history (`history`) and `compare`: before/after report with bootstrap confidence intervals, `--matched`, `--markdown` and `--json`.
- `context` command: what makes up the fixed load at the start of a session.
- Model routing: `scout`, `worker`, `architect` and `expert` subagents, default subagent model, model advice at the first prompt.
- English and Turkish output.
- `cc-optimizer --version`.
- On-hold LLM API gateway (`llm-gateway`), installable with the `gateway` extra.

### Changed
- The package is now published as `service001` (it was `llm-gateway`) and has no required dependencies. `tiktoken` is optional (`accurate` extra), the gateway dependencies moved to the `gateway` extra.
- The version is defined once, in `src/cc_optimizer/__init__.py`.
