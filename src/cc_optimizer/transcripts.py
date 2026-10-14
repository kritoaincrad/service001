"""Parse Claude Code session transcripts (~/.claude/projects/<project>/<session>.jsonl).

The format is not a documented public contract, so parsing is defensive: unknown
entries are skipped and missing fields default to zero.

Each transcript file is one context timeline. The main session and every subagent
have their own file and therefore their own context window.

Every piece of content that enters the context (a tool output, a user prompt, an
attachment, a compaction summary) is stored as a ContextItem with:
  start: index of the first API call that sees it
  end:   index of the first API call that no longer sees it (next compaction or end)
so that `end - start` is the number of calls that carried it.
No prompt or tool output text is kept, only token estimates.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

# Rough token cost of one image. Real cost depends on resolution.
IMAGE_TOKENS = 1500

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}


class TokenCounter:
    """tiktoken o200k_base when available, otherwise ~4 chars per token.

    Claude uses a different tokenizer, so counts are calibrated later against
    the real context growth recorded in the transcript.
    """

    def __init__(self, fast: bool = False) -> None:
        self._enc = None
        self.method = "chars/4"
        if not fast:
            try:
                import tiktoken

                self._enc = tiktoken.get_encoding("o200k_base")
                self.method = "tiktoken:o200k_base"
            except Exception as exc:
                log.warning("tiktoken unavailable (%s); using a 4 chars/token estimate", exc)

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._enc is not None:
            return len(self._enc.encode(text, disallowed_special=()))
        return max(1, round(len(text) / 4))


@dataclass
class ApiCall:
    index: int
    model: str
    timestamp: str | None
    input: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output: int = 0
    # Estimated tokens of the visible output (text and tool calls), without thinking,
    # split by kind: "text" or the name of the tool being called.
    visible_parts: dict[str, int] = field(default_factory=dict)

    @property
    def visible_output_est(self) -> int:
        return sum(self.visible_parts.values())

    @property
    def context(self) -> int:
        return self.input + self.cache_creation + self.cache_read

    @property
    def total(self) -> int:
        return self.context + self.output


@dataclass
class ContextItem:
    kind: str  # tool | prompt | attachment | summary
    name: str  # tool name, attachment type, ...
    tokens: int
    start: int
    end: int = -1
    descriptor: str = ""
    images: int = 0
    is_error: bool = False
    result_type: str | None = None  # Read: text | image | file_unchanged | pdf
    path: str | None = None  # Read: normalized file path
    read_range: tuple | None = None  # Read: (offset, limit)
    group: str | None = None  # shell tools: program and subcommand

    @property
    def carried_calls(self) -> int:
        return max(self.end - self.start, 0)

    @property
    def carried_tokens(self) -> int:
        return self.tokens * self.carried_calls


@dataclass
class Compaction:
    position: int
    trigger: str | None
    pre_tokens: int | None
    post_tokens: int | None


@dataclass
class Timeline:
    path: str
    project: str
    session_id: str
    is_subagent: bool
    title: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    calls: list[ApiCall] = field(default_factory=list)
    items: list[ContextItem] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    edits: list[tuple[int, str]] = field(default_factory=list)  # (position, normalized path)


# --------------------------------------------------------------------- discovery


def project_dir_name(path: str | Path) -> str:
    """Claude Code names a project folder after its path with non-alphanumerics as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def find_transcripts(
    root: Path, project: str | None = None, session: str | None = None, include_subagents: bool = True
) -> Iterator[tuple[Path, str, str, bool]]:
    """Yield (file, project folder, session id, is_subagent)."""
    if not root.is_dir():
        return
    wanted = project_dir_name(project).lower() if project else None
    for proj in sorted(p for p in root.iterdir() if p.is_dir()):
        if wanted and proj.name.lower() != wanted:
            continue
        for main in sorted(proj.glob("*.jsonl")):
            sid = main.stem
            if session and not sid.startswith(session):
                continue
            yield main, proj.name, sid, False
            if include_subagents:
                for sub in sorted((proj / sid / "subagents").glob("*.jsonl")):
                    yield sub, proj.name, sid, True


# ------------------------------------------------------------------------ helpers


def normalize_path(p: Any) -> str | None:
    if not isinstance(p, str) or not p:
        return None
    return p.replace("\\", "/").lower()


def _one_line(s: Any, limit: int = 100) -> str:
    text = " ".join(str(s).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_tool(name: str, inp: dict) -> str:
    if name in SHELL_TOOLS:
        return _one_line(inp.get("command", ""))
    if name == "Read" or name in EDIT_TOOLS:
        target = inp.get("file_path") or inp.get("notebook_path") or ""
        extra = ""
        if name == "Read" and (inp.get("offset") or inp.get("limit")):
            extra = f" [{inp.get('offset') or 0}:+{inp.get('limit') or '…'}]"
        return _one_line(f"{target}{extra}")
    if name == "Grep":
        return _one_line(f"{inp.get('pattern', '')} @ {inp.get('path') or inp.get('glob') or '.'}")
    if name == "Glob":
        return _one_line(inp.get("pattern", ""))
    if name == "WebFetch":
        return _one_line(inp.get("url", ""))
    if name == "WebSearch":
        return _one_line(inp.get("query", ""))
    if name in ("Agent", "Task"):
        return _one_line(inp.get("description") or inp.get("subagent_type") or "")
    return _one_line(json.dumps(inp, ensure_ascii=False, default=str), 80)


_SUBCOMMAND_TOOLS = {
    "git", "npm", "pnpm", "yarn", "npx", "python", "python3", "py", "pip", "uv", "node", "dotnet",
    "go", "cargo", "docker", "kubectl", "mvn", "gradle", "make", "poetry",
}
_SKIP_PROGRAMS = {"cd", "export", "set", "pushd", "popd", "set-location", "push-location", "sl"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n|\|")


def _basename(token: str) -> str:
    return re.split(r"[\\/]", token.strip("\"'"))[-1]


def command_group(command: str) -> str:
    """Group shell commands by program and subcommand, e.g. 'git diff', 'npm test'.

    Uses the first segment of the command that is not a `cd` or a variable assignment.
    """
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = segment.split()
        while tokens and _ASSIGNMENT.match(tokens[0]):
            tokens.pop(0)
        if not tokens:
            continue
        prog = re.sub(r"\.(exe|cmd|bat)$", "", _basename(tokens[0]).lower())
        if not prog or prog in _SKIP_PROGRAMS or prog.startswith("#"):
            continue
        if prog in _SUBCOMMAND_TOOLS:
            for t in tokens[1:]:
                if t[0] in "-<>&$(" or "=" in t:
                    continue
                return f"{prog} {_basename(t)}"
        return prog
    return "cd" if command.strip().startswith("cd") else "(boş)"


def _attachment_text(obj: Any, texts: list[str]) -> int:
    """Collect the text of an attachment. Base64 payloads count as images, not text."""
    images = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("data", "base64") and isinstance(value, str) and len(value) > 500:
                images += 1
            else:
                images += _attachment_text(value, texts)
    elif isinstance(obj, list):
        for value in obj:
            images += _attachment_text(value, texts)
    elif isinstance(obj, str):
        texts.append(obj)
    return images


# Attachments that record client state and never reach the model as content.
NON_CONTEXT_ATTACHMENTS = {"prompt_snapshot"}


def _content_parts(content: Any) -> tuple[str, int]:
    """Return (text, image count) of a message or tool_result content."""
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        return "", 0
    texts, images = [], 0
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            texts.append(part.get("text") or "")
        elif ptype in ("image", "document"):
            images += 1
        elif ptype == "tool_reference":
            texts.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(texts), images


# ------------------------------------------------------------------------ parsing


def parse_timeline(
    path: Path, project: str, session_id: str, is_subagent: bool, counter: TokenCounter
) -> Timeline:
    tl = Timeline(str(path), project, session_id, is_subagent)
    calls_by_request: dict[str, ApiCall] = {}
    tool_uses: dict[str, tuple[str, dict]] = {}
    open_items: list[ContextItem] = []
    custom_title: str | None = None

    def add(item: ContextItem) -> None:
        tl.items.append(item)
        open_items.append(item)

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type")
            ts = entry.get("timestamp")
            if isinstance(ts, str):
                tl.first_ts = tl.first_ts or ts
                tl.last_ts = ts
            position = len(tl.calls)

            if etype == "custom-title" and entry.get("customTitle"):
                custom_title = entry["customTitle"]
            elif etype == "ai-title" and entry.get("aiTitle"):
                tl.title = entry["aiTitle"]

            elif etype == "assistant":
                msg = entry.get("message") or {}
                model = msg.get("model") or "?"
                if model == "<synthetic>":
                    continue
                request_id = entry.get("requestId") or msg.get("id") or entry.get("uuid")
                call = calls_by_request.get(request_id)
                if call is None:
                    call = ApiCall(index=len(tl.calls), model=model, timestamp=ts)
                    tl.calls.append(call)
                    calls_by_request[request_id] = call
                # One response is written as several lines; keep the largest numbers.
                usage = msg.get("usage") or {}
                call.input = max(call.input, int(usage.get("input_tokens") or 0))
                call.cache_creation = max(call.cache_creation, int(usage.get("cache_creation_input_tokens") or 0))
                call.cache_read = max(call.cache_read, int(usage.get("cache_read_input_tokens") or 0))
                call.output = max(call.output, int(usage.get("output_tokens") or 0))
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        n = counter.count(block.get("text") or "")
                        call.visible_parts["text"] = call.visible_parts.get("text", 0) + n
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        kind = str(block.get("name") or "?")
                        n = counter.count(f"{kind} {json.dumps(block.get('input'), ensure_ascii=False, default=str)}")
                        call.visible_parts[kind] = call.visible_parts.get(kind, 0) + n
                        name = block.get("name") or "?"
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        tool_uses[block.get("id")] = (name, inp)
                        if name in EDIT_TOOLS:
                            target = normalize_path(inp.get("file_path") or inp.get("notebook_path"))
                            if target:
                                tl.edits.append((len(tl.calls), target))

            elif etype == "user":
                msg = entry.get("message") or {}
                content = msg.get("content")
                if entry.get("isCompactSummary"):
                    text, _ = _content_parts(content)
                    add(ContextItem("summary", "compaction_summary", counter.count(text), position))
                    continue
                if isinstance(content, str):
                    if not entry.get("isMeta"):
                        add(ContextItem("prompt", "user_prompt", counter.count(content), position))
                    continue
                if not isinstance(content, list):
                    continue
                result_meta = entry.get("toolUseResult") if isinstance(entry.get("toolUseResult"), dict) else {}
                prompt_texts, prompt_images = [], 0
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_result":
                        name, inp = tool_uses.get(block.get("tool_use_id"), ("?", {}))
                        text, images = _content_parts(block.get("content"))
                        item = ContextItem(
                            "tool",
                            name,
                            counter.count(text) + images * IMAGE_TOKENS,
                            position,
                            descriptor=describe_tool(name, inp),
                            images=images,
                            is_error=bool(block.get("is_error")),
                        )
                        if name in SHELL_TOOLS:
                            item.group = command_group(str(inp.get("command") or ""))
                        if name == "Read":
                            item.result_type = result_meta.get("type") if isinstance(result_meta.get("type"), str) else None
                            item.path = normalize_path(inp.get("file_path"))
                            item.read_range = (inp.get("offset"), inp.get("limit"))
                        add(item)
                    elif btype == "text" and not entry.get("isMeta"):
                        prompt_texts.append(block.get("text") or "")
                    elif btype in ("image", "document"):
                        prompt_images += 1
                if prompt_texts or prompt_images:
                    text = "\n".join(prompt_texts)
                    add(ContextItem("prompt", "user_prompt", counter.count(text) + prompt_images * IMAGE_TOKENS,
                                    position, images=prompt_images))

            elif etype == "attachment":
                att = entry.get("attachment") if isinstance(entry.get("attachment"), dict) else {}
                att_type = str(att.get("type") or "?")
                if att_type in NON_CONTEXT_ATTACHMENTS:
                    continue
                # `rendered` holds what the model actually receives, when it is recorded.
                rendered = entry.get("rendered")
                texts: list[str] = []
                images = _attachment_text(rendered if isinstance(rendered, list) and rendered else att, texts)
                tokens = counter.count("\n".join(texts)) + images * IMAGE_TOKENS
                add(ContextItem("attachment", att_type, tokens, position, images=images))

            elif etype == "system" and entry.get("subtype") == "compact_boundary":
                meta = entry.get("compactMetadata") if isinstance(entry.get("compactMetadata"), dict) else {}
                tl.compactions.append(
                    Compaction(position, meta.get("trigger") or entry.get("trigger"), meta.get("preTokens"), meta.get("postTokens"))
                )
                for item in open_items:
                    item.end = position
                open_items.clear()

    for item in open_items:
        item.end = len(tl.calls)
    tl.title = custom_title or tl.title
    return tl
