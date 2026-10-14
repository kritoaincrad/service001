"""Request analysis: where do the input tokens go?

Runs on every request without storing prompt content. Only token counts,
hashes and a language label leave this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .config import ApiType

log = logging.getLogger(__name__)

SEGMENTS = ("system", "tools", "history", "tool_results", "user_prompt")

# Keys that do not change what the model sees and are excluded from the prompt hash.
_HASH_IGNORED_KEYS = {"stream", "stream_options", "user", "metadata", "store", "service_tier"}

_TR_STRONG = set("ğĞışŞİ")
_TR_WORDS = {
    "ve", "bir", "bu", "için", "ile", "da", "de", "ne", "mi", "çok", "olarak",
    "gibi", "daha", "değil", "var", "yok", "nasıl", "neden", "ama", "veya", "şu",
}
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_LANG_SAMPLE_CHARS = 20_000


class TokenCounter:
    """Counts tokens with tiktoken; falls back to a rough 4 chars per token."""

    def __init__(self, encoding: str = "o200k_base") -> None:
        self.method = f"tiktoken:{encoding}"
        self._enc = None
        try:
            import tiktoken

            self._enc = tiktoken.get_encoding(encoding)
        except Exception as exc:  # network blocked, unknown encoding, ...
            log.warning("tiktoken unavailable (%s); using a 4 chars/token estimate", exc)
            self.method = "chars/4"

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._enc is not None:
            return len(self._enc.encode(text, disallowed_special=()))
        return max(1, round(len(text) / 4))


@dataclass
class RequestAnalysis:
    model: str | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    segments: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SEGMENTS, 0))
    message_count: int = 0
    attachment_count: int = 0
    prompt_hash: str | None = None
    prefix_hash: str | None = None
    user_prompt_hash: str | None = None
    lang: str = "unknown"
    text_chars: int = 0
    text_tokens: int = 0
    est_method: str = ""

    @property
    def est_input_tokens(self) -> int:
        return sum(self.segments.values())

    def as_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {f"est_{s}_tokens": n for s, n in self.segments.items()}
        rec.update(
            est_input_tokens=self.est_input_tokens,
            message_count=self.message_count,
            attachment_count=self.attachment_count,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            prompt_hash=self.prompt_hash,
            prefix_hash=self.prefix_hash,
            user_prompt_hash=self.user_prompt_hash,
            lang=self.lang,
            text_chars=self.text_chars,
            text_tokens=self.text_tokens,
            est_method=self.est_method,
        )
        return rec


def _hash(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def detect_language(text: str) -> str:
    """Cheap heuristic: 'tr', 'other' or 'unknown'. Good enough for traffic shares."""
    sample = text[:_LANG_SAMPLE_CHARS]
    letters = sum(ch.isalpha() for ch in sample)
    if letters < 20:
        return "unknown"
    strong = sum(ch in _TR_STRONG for ch in sample)
    words = [w.lower() for w in _WORD_RE.findall(sample)]
    word_hits = sum(w in _TR_WORDS for w in words)
    if strong / letters >= 0.005 or (words and word_hits / len(words) >= 0.05):
        return "tr"
    return "other"


class _Collector:
    def __init__(self) -> None:
        self.parts: dict[str, list[str]] = {s: [] for s in SEGMENTS}
        self.attachments = 0

    def add(self, segment: str, text: Any) -> None:
        if isinstance(text, str) and text:
            self.parts[segment].append(text)


def _openai_content(content: Any, segment: str, col: _Collector) -> None:
    if isinstance(content, str):
        col.add(segment, content)
        return
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("text", "input_text", "output_text"):
            col.add(segment, part.get("text"))
        elif ptype in ("image_url", "input_image", "input_audio", "file", "input_file"):
            col.attachments += 1


def _collect_openai(body: dict, col: _Collector) -> int:
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    last_user = max((i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"), default=-1)
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("system", "developer"):
            seg = "system"
        elif role in ("tool", "function"):
            seg = "tool_results"
        elif i == last_user:
            seg = "user_prompt"
        else:
            seg = "history"
        _openai_content(m.get("content"), seg, col)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict):
                col.add("history", f"{fn.get('name', '')} {fn.get('arguments', '')}")
        if isinstance(m.get("function_call"), dict):
            col.add("history", _dump(m["function_call"]))

    for key in ("tools", "functions"):
        if body.get(key):
            col.add("tools", _dump(body[key]))
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_schema":
        col.add("tools", _dump(rf))
    return len(messages)


def _anthropic_blocks(content: Any, segment: str, col: _Collector) -> None:
    if isinstance(content, str):
        col.add(segment, content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            col.add(segment, block.get("text"))
        elif btype in ("tool_use", "server_tool_use"):
            col.add("history", f"{block.get('name', '')} {_dump(block.get('input', {}))}")
        elif btype == "tool_result":
            _anthropic_blocks(block.get("content"), "tool_results", col)
        elif btype in ("image", "document"):
            col.attachments += 1
        elif btype == "thinking":
            col.add("history", block.get("thinking"))


def _collect_anthropic(body: dict, col: _Collector) -> int:
    _anthropic_blocks(body.get("system"), "system", col)
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    last_user = max((i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"), default=-1)
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        seg = "user_prompt" if i == last_user else "history"
        _anthropic_blocks(m.get("content"), seg, col)
    if body.get("tools"):
        col.add("tools", _dump(body["tools"]))
    return len(messages)


def analyze_request(body: dict, api: ApiType, counter: TokenCounter) -> RequestAnalysis:
    col = _Collector()
    if api == "anthropic":
        message_count = _collect_anthropic(body, col)
    else:
        message_count = _collect_openai(body, col)

    seg_text = {s: "\n".join(parts) for s, parts in col.parts.items()}
    segments = {s: counter.count(t) for s, t in seg_text.items()}

    # Natural-language text only: tool schemas and tool output would skew language stats.
    natural = "\n".join(seg_text[s] for s in ("system", "history", "user_prompt") if seg_text[s])

    hashable = {k: v for k, v in body.items() if k not in _HASH_IGNORED_KEYS}
    prefix_parts = [seg_text["system"], seg_text["tools"]]
    user_norm = " ".join(seg_text["user_prompt"].lower().split())

    max_tokens = body.get("max_tokens", body.get("max_completion_tokens", body.get("max_output_tokens")))
    temperature = body.get("temperature")
    return RequestAnalysis(
        model=body.get("model") if isinstance(body.get("model"), str) else None,
        stream=bool(body.get("stream")),
        max_tokens=max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) else None,
        temperature=float(temperature) if isinstance(temperature, (int, float)) and not isinstance(temperature, bool) else None,
        segments=segments,
        message_count=message_count,
        attachment_count=col.attachments,
        prompt_hash=_hash(hashable),
        prefix_hash=_hash(prefix_parts) if any(prefix_parts) else None,
        user_prompt_hash=_hash(user_norm) if user_norm else None,
        lang=detect_language(natural),
        text_chars=len(natural),
        text_tokens=counter.count(natural),
        est_method=counter.method,
    )
