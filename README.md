# Service001

English | [Türkçe](README.tr.md)

A toolkit that measures and reduces Claude Code token consumption.
See [ROADMAP.md](ROADMAP.md) for the product vision, measurement findings and phases.

## Installation

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
# .venv/bin/python -m pip install -e ".[dev]"     # Linux / macOS
```

## Claude Code token analysis

Claude Code records every session under `~/.claude/projects`.
The analysis reads these records locally, sends nothing out and does not modify the records.

```bash
.venv/Scripts/cc-optimizer analyze                  # all projects
.venv/Scripts/cc-optimizer analyze --project .      # only the project of the current folder
.venv/Scripts/cc-optimizer analyze --days 7         # sessions active in the last 7 days
.venv/Scripts/cc-optimizer analyze --session 3a54   # a single session, by the start of its ID
.venv/Scripts/cc-optimizer analyze --json           # machine-readable output
```

Report sections:

1. **Token consumption:** Cache reads, cache writes, input and output, per model.
2. **Context size:** Context per call, the fixed load at startup, compactions and an earlier-compaction scenario.
3. **What fills the context:** The size of each source and for how many calls it was carried. A breakdown of the content the model wrote.
4. **Most expensive tool outputs:** The individual reads and commands that put the most load on the context.
5. **Shell commands:** Output load by program and subcommand.
6. **File reads:** Unnecessary repeat reads, unbounded large reads, the most-read files.
7. **Most consuming sessions.**

### Limits of the measurement

- Token counts of model calls are the actual values from the record.
- Sizes per source are estimated with `o200k_base`, then calibrated against the actual context growth. The report shows how much of the actual consumption it can explain.
- Images are counted as a fixed 1,500 tokens.
- Thinking tokens are assumed not to stay in the context in later calls.
- The compaction scenario is a rough estimate. It assumes that after compaction the work would proceed in the same way.
- The record format is Claude Code's internal format and may change between versions.

## Context guards

Guards that run through Claude Code hooks and catch behaviors that bloat the context.
They do not block anything; they only inform Claude and you. The one exception is large reads: there, refusing once saves tokens.

| Guard | When | What it does |
|---|---|---|
| Session rules | At session start, after `/clear` and after compaction | Adds a three-line token rule to Claude |
| Context warning | When the context exceeds 300 thousand tokens, then every 100 thousand | Suggests `/compact` or `/clear` to you, tells Claude to keep the work focused |
| Rewrite warning | When an existing large file is rewritten with Write while most of its content stays the same | Tells Claude to edit only the changed lines in later changes |
| Heredoc warning | The same situation done with `cat > file <<EOF` | The same warning |
| Large read | When a file larger than about 6 thousand tokens is read without a range | Refuses once, gives a line-numbered outline of the file and tells Claude to find the spot with Grep and read a range. If Claude repeats the same read, that file is allowed for the rest of that session |
| Dumping files in the shell | When files whose total exceeds the threshold are dumped in full with `cat a.tsx b.tsx` or `Get-Content` | Refuses once, like the large read. Does not touch ranged dumps (`sed -n`, `head`), or output piped to another command or redirected to a file |
| Status line | Always | Model, context size and session cost, colored by threshold |

Install and uninstall:

```bash
.venv/Scripts/cc-optimizer install --scope local --statusline        # this project only
.venv/Scripts/cc-optimizer install --scope user --statusline         # all projects
.venv/Scripts/cc-optimizer install --scope user --compact-window 400000   # also the compaction threshold
.venv/Scripts/cc-optimizer install --scope user --bash-output-max 12000   # long shell output goes to a file
.venv/Scripts/cc-optimizer install --scope user --dry-run            # show before writing
.venv/Scripts/cc-optimizer uninstall --scope user                    # restores everything
```

The installation preserves existing settings, takes a backup first and restores the values it changed when uninstalled.

When the guards hit an error, they silently disable themselves so they do not break Claude Code. That is why you should check occasionally:

```bash
.venv/Scripts/cc-optimizer doctor
```

What it checks:
- Whether the hooks are installed and up to date.
- Whether the Python path is valid. The hooks and the status line are actually run and their durations measured.
- Whether the guards have really run while Claude Code was in use. Each time a hook runs, it leaves a timestamp under `~/.claude/cc-optimizer/heartbeat/`.
- Whether `config.json` is valid.
- Whether the subagents are up to date.
- Whether there is a second installation in this project.
- Whether the project map is in place.

If there is a problem, it explains how to fix it and exits with 1 on failure.

Settings can be changed in `~/.claude/cc-optimizer/config.json`:

```json
{
  "context_warn_tokens": 300000,
  "context_step_tokens": 100000,
  "write_guard": "coach",
  "write_min_lines": 120,
  "write_unchanged_ratio": 0.6,
  "heredoc_guard": true,
  "read_guard": "block",
  "read_guard_tokens": 6000,
  "session_guidance": true
}
```

If `"block"` is chosen for `write_guard`, a rewrite is refused once. If Claude tries the same write again, it is allowed.
This mode is not the default, because the refused content has already been generated and has entered the context.

For `read_guard` the situation is the reverse. When a read is refused, the file has not entered the context yet.
In the first measurement, 71 unbounded reads over 6 thousand tokens carried a total of 223 million tokens in later calls.
The extra calls caused by refusing cost 22 million tokens.
`"coach"` only warns, `"off"` disables it. The same setting also controls dumping files in the shell.

There is no separate guard for long shell output, because Claude Code does this itself.
When `bashOutputMaxChars` is exceeded, the output is written to a file and Claude sees only a preview and the file path.
The default limit is about 30 thousand characters. `--bash-output-max` lowers it and restores the old value when removed.

To measure the effect of the guards:

```bash
.venv/Scripts/cc-optimizer events --days 7      # what the guards did
.venv/Scripts/cc-optimizer compare              # before / after comparison against the baseline measurement
```

`compare` compares the newest baseline measurement in `data/` with the sessions that started after it.
Because the work in the two periods differs, it compares per-call values, medians and shares, not totals.
The main metric is tokens processed per call. It warns if the later period has fewer than 5 sessions or 1,000 calls.

## Work log (project memory)

In a new chat or after the context is compacted, Claude does not know the earlier work.
Service001 keeps a work log for each project in `.claude/worklog.md`. For each session it records:
- Your requests.
- The project files that changed and how many times each was edited.
- The commands that ran. Read commands excluded.
- Claude's final answer.

Entries are extracted from the session records by rules, so no extra tokens are spent.
Pasted content, IDE notes and files outside the project are filtered out. The newest 40 sessions are kept.

What enters the context:

| When | What enters the context |
|---|---|
| New chat | The path of the log and a one-line summary of the last 3 sessions (date, title, changed files), about 150 tokens |
| After the context is compacted | The same, plus a one-line summary of what was done in this session up to the compaction |

When Claude continues an earlier piece of work, it reads the relevant entry in the log.
The previous session is written to the log immediately when a new session opens. The others are processed in the background sync.
Like the map, the log is kept out of git through `.git/info/exclude`, on this computer only.
It is turned off with `"worklog": false` in `config.json`, and `"worklog_recent"` sets the number of sessions that enter the context.

Claude Code's own memory (CLAUDE.md and automatic memory) holds decisions and rules. The work log holds what was done. The two complement each other.

## Token history

Claude Code deletes session records after about 30 days. Service001 permanently stores only the numbers of each session.
Stored numbers: project, date, calls, tokens processed, context median, model distribution, exploration load, read and shell shares.
The record is in `~/.claude/cc-optimizer/history.db` and no content is stored.

The record is updated in the background at session start, at most once an hour. Only new and changed sessions are processed.
Sessions whose records have been deleted stay in the history.

Each session is labeled by looking at its own record:

| Label | Meaning |
|---|---|
| With Service001 | The text added by the guards is present before the first model call |
| Mixed | The guards were installed in the middle of the session. Not included in comparison |
| Without Service001 | No trace of the guards |

```bash
.venv/Scripts/cc-optimizer history                     # token history by week and project
.venv/Scripts/cc-optimizer history --project KEIMORA   # a single project
.venv/Scripts/cc-optimizer compare --project KEIMORA   # sessions without and with Service001 in KEIMORA
.venv/Scripts/cc-optimizer compare                     # all projects
.venv/Scripts/cc-optimizer compare --matched           # only projects that have sessions on both sides
.venv/Scripts/cc-optimizer compare --markdown          # a Markdown table for a pull request or a team update
```

`compare` now uses this history by default. For the old file-based comparison, pass `--baseline data/baseline-2026-10-14-v2.json`.

How to read the report:

- **Confidence.** Every row is checked against the session-to-session noise (a bootstrap over sessions, 90% interval, fixed seed). A change that cannot be told apart from noise is shown as `unclear`, not as better or worse. With fewer than 3 sessions on a side nothing can be said. The interval is printed next to the change.
- **Different projects.** If the two periods mostly cover different projects (overlap under 50%), the report warns that the difference may come from the work. `--matched` keeps only projects present on both sides; `--project NAME` looks at one.
- **What the guards kept out.** The report adds the tokens that blocking guards kept out of the context at the first attempt, taken from the event log. It is an upper bound, because a blocked read can be retried in smaller parts.
- `compare --json` contains the same data (`confidence`, `interval` per row, `projects`, `guard_effect`).

## Project map

At the start of every session, if the opened folder is a project, `.claude/project-map.md` is generated or updated.
For a folder to count as a project, it must contain a marker such as git, `package.json` or `pyproject.toml`.

| Content | Source |
|---|---|
| Project summary | The first paragraph of the README |
| Run commands | `package.json` scripts, `pyproject.toml` |
| Folder structure | `git ls-files` in a git repository, otherwise a folder scan. Folders such as `node_modules` and `dist` are skipped |
| File descriptions | For projects up to 150 files, one line from the header comment of each file. For large projects, folders with their file counts |
| Pages and routes | The answer to "which file is this page in": route, file, component and what the page uses. Pages of Next, SvelteKit, Nuxt, Astro, React Router, Angular, Vue Router, Razor/MVC and Blazor. For APIs: Express, NestJS, Flask, FastAPI, Django, Spring, ASP.NET, Laravel, Go, Rails |
| Search index | `.claude/project-index.md`: `name  file:line  kind` for every route, page and top-level symbol. Claude finds things with a single `grep` |
| Dependencies and hubs | Which files each file imports and which import it (JS/TS with tsconfig `paths`, Vue, Svelte, Python, Java, Kotlin, Go, C#), and the files and symbols the most code depends on. Answers "what is affected if I change this" |

The map itself is not added to the context, only its path. Everything added to the context is re-read on every call. A file, on the other hand, is read once when needed.
It is regenerated if there is no map, if it is older than 6 hours, if there is a new commit in git, or if files were added or deleted.
Detection is done with regular expressions and is not a parser: if it misses something, Claude does a normal search. Nested routes are not merged with their parent route.
Dependencies come from static imports. Go, C#, Java and Kotlin connect files through packages and namespaces, so there a file is linked to another when it mentions a type (or an exported Go function) that is defined in one place within a scope it can see; Rust, PHP and Ruby have no dependency edges yet. Dynamic imports and reflection are not seen. Symbol references are counted by name: a name that is defined in several files or is very generic is not counted.
So that it does not enter git, it is added only to `.git/info/exclude` on this computer. The shared `.gitignore` is unchanged.

```bash
.venv/Scripts/cc-optimizer map            # generate this folder's map now
.venv/Scripts/cc-optimizer map --print    # show without writing
cc-optimizer impact src/lib/api.ts       # files affected by a change (name alone works when unique)
cc-optimizer map --graph > graph.json    # files, imports, importers, symbols, routes as plain JSON
```

**In a session that is already open.** The map is normally created when a session starts. For a session that was open before the guards were installed, there are two ways in, no restart needed:

- The first prompt after the hooks become active announces the map once (the hook creates it if it is missing). If the hooks were installed while the session was open, open Claude Code's `/hooks` menu once so the session reloads them.
- The `/project-map` command (added by `install`) refreshes the map and the index on demand and tells Claude to use them. It asks Claude to run `cc-optimizer map --announce`, which you can also run yourself in a terminal.

Only paths and usage hints enter the context, never the map itself.

It is turned off with `"project_map": false` in `config.json`.

For project-specific settings you can write `.claude/cc-optimizer.json` (it can be committed and shared with the team):

```json
{
  "ignore": ["legacy/**", "*.generated.ts"],
  "routes": [
    {"files": "src/**/*.ts", "kind": "page", "regex": "registerPage\\('(?P<path>[^']+)', '(?P<name>\\w+)'"}
  ]
}
```

`ignore` removes files from the map. `routes` adds your own route pattern: the `path` group is required in the regex, `method` and `name` are optional. When a file changes, the map is refreshed.

## Language

Output is supported in English and Turkish. Selection order: the `CC_OPTIMIZER_LANG` environment variable (`en` or `tr`), `"language": "en" | "tr" | "auto"` in `config.json`, the system language (Turkish if the system is Turkish), otherwise English. Text sent to Claude is always in English.
The "Exploration before the first edit, median" line in the `compare` output measures its effect. At baseline the median is about 10.6 thousand tokens.

## Model routing

Claude Code does not let hooks change the model of the main chat.
Switching the model in the middle of a conversation is also expensive, because each model has its own cache and the whole conversation is reprocessed.
That is why routing is done at two points.

| Part | What it does |
|---|---|
| Default model | New chats start with Sonnet (`--model sonnet`). If a whole session will be hard, `/model opus` is used. When removed, the previous model comes back |
| Subagents | Down: `scout` (Haiku) for search and reading, `worker` (Sonnet) for clearly defined coding. Up: `architect` (Opus) for architecture, hard debugging and risky multi-file changes, `expert` (Fable) for the hardest problems. Because a subagent starts from scratch, no cache is lost |
| Session-start rules | Tell Claude which work to give to which agent. Added only if the agents are installed |
| Exploration reminder | In the call after the main chat has made 4 searches or reads in a row, if it will continue exploring, it tells Claude to delegate to scout (Haiku). The outputs of the exploration do not pile up in the chat; only a short report comes back. Done once per exploration sequence. If Claude ignores 3 reminders, it stops for that session. Not applied to the subagents' own calls. The threshold is changed with the `routing_nudge_after` setting |
| Default subagent model | Subagents that do not specify a model run with this model instead of Opus |
| Model suggestion in the first prompt | In the session's first prompt, if the work is simple, Claude suggests `/model haiku` or `/model sonnet` to you. This is the only moment when switching is cheap |

```bash
.venv/Scripts/cc-optimizer install --scope user --agents --subagent-model sonnet
```

In the first measurement, 95% of consumption was in the main chat, and 78 of the subagents' 83 calls inherited Opus.
The biggest effect comes from starting the session with the model that suits the work.
The `model_routing` and `model_advice` settings can be turned off in `config.json`.

## On hold: LLM API gateway

`src/llm_gateway` is a measurement proxy written for organizations that use an LLM API in their own applications.
It forwards OpenAI- and Anthropic-compatible requests as they are and records tokens, cost and latency.
It is not the current priority.

```bash
cp config.example.yaml config.yaml
.venv/Scripts/llm-gateway serve --config config.yaml --port 8080
.venv/Scripts/llm-gateway report --days 7
```

Provider keys are read from the `.env` file. This file and `config.yaml` do not go into git.

## Tests

```bash
.venv/Scripts/python -m pytest -q
```

## Structure

```
src/cc_optimizer/
  transcripts.py  parsing session records
  analyze.py      sources, carried load, calibration, scenario
  report.py       text report (English / Turkish)
  compare.py      before / after comparison
  hooks.py        context guards (hook handlers)
  statusline.py   status line
  installer.py    installing into and removing from the settings file
  guard_config.py guard settings, session state, event log
  shell_reads.py  detecting full file dumps in shell commands
  project_map.py  project map
  code_index.py   page, route and symbol detection
  i18n.py         English / Turkish text selection
  doctor.py       health check
  history.py      persistent token history
  worklog.py      project work log
  agents/         scout, worker, expert subagent definitions
  cli.py          command line
src/llm_gateway/  the on-hold API gateway
tests/
```
