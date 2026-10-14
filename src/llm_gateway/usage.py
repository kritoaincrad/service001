"""Extract token usage and metadata from provider responses, streaming or not."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config import ApiType


@dataclass
class Usage:
    # Normalized: input_total always includes cache reads and cache writes.
    input_total: int | None = None
    output: int | None = None
    cached_input: int = 0
    cache_write: int = 0
    reasoning: int = 0
    source: str = "provider"  # "provider" or "estimated"


@dataclass
class ResponseInfo:
    usage: Usage | None = None
    response_model: str | None = None
    finish_reason: str | None = None
    error_type: str | None = None


def _int(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v)


def parse_usage(u: Any, flavor: ApiType) -> Usage | None:
    if not isinstance(u, dict):
        return None
    if flavor == "anthropic":
        if "input_tokens" not in u and "output_tokens" not in u:
            return None
        base = _int(u.get("input_tokens"))
        cache_read = _int(u.get("cache_read_input_tokens"))
        cache_write = _int(u.get("cache_creation_input_tokens"))
        return Usage(
            input_total=base + cache_read + cache_write,
            output=_int(u.get("output_tokens")),
            cached_input=cache_read,
            cache_write=cache_write,
        )
    # OpenAI: Chat Completions uses prompt/completion, Responses uses input/output.
    # In both, the input count already includes cached tokens.
    inp = u.get("prompt_tokens", u.get("input_tokens"))
    out = u.get("completion_tokens", u.get("output_tokens"))
    if inp is None and out is None:
        return None
    in_det = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    out_det = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
    return Usage(
        input_total=_int(inp),
        output=_int(out),
        cached_input=_int(in_det.get("cached_tokens")) if isinstance(in_det, dict) else 0,
        reasoning=_int(out_det.get("reasoning_tokens")) if isinstance(out_det, dict) else 0,
    )


def _error_type(body: dict) -> str | None:
    err = body.get("error")
    if isinstance(err, dict):
        t = err.get("type") or err.get("code")
        return str(t) if t else "error"
    if isinstance(err, str):
        return err
    return None


def info_from_json(body: Any, flavor: ApiType) -> ResponseInfo:
    if not isinstance(body, dict):
        return ResponseInfo()
    info = ResponseInfo(
        usage=parse_usage(body.get("usage"), flavor),
        response_model=body.get("model") if isinstance(body.get("model"), str) else None,
        error_type=_error_type(body),
    )
    if flavor == "anthropic":
        info.finish_reason = body.get("stop_reason")
    else:
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            info.finish_reason = choices[0].get("finish_reason")
        elif isinstance(body.get("status"), str):
            info.finish_reason = body["status"]
    return info


def parse_sse_event(raw: bytes) -> tuple[str | None, str | None]:
    """Return (event name, joined data) for one SSE event block."""
    event: str | None = None
    data_lines: list[str] = []
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    return event, ("\n".join(data_lines) if data_lines else None)


@dataclass
class StreamTracker:
    """Observes the SSE events of a streaming response.

    feed() returns False for events that must not reach the client: the usage-only
    chunk that the gateway requested on behalf of the client.
    """

    flavor: ApiType
    strip_usage_only_chunks: bool = False
    usage: Usage | None = None
    response_model: str | None = None
    finish_reason: str | None = None
    error_type: str | None = None
    _text: list[str] = field(default_factory=list)
    _anthropic_usage: dict = field(default_factory=dict)

    @property
    def output_text(self) -> str:
        return "".join(self._text)

    def info(self) -> ResponseInfo:
        return ResponseInfo(self.usage, self.response_model, self.finish_reason, self.error_type)

    def feed(self, raw_event: bytes) -> bool:
        _, data = parse_sse_event(raw_event)
        if not data or data.strip() == "[DONE]":
            return True
        try:
            obj = json.loads(data)
        except ValueError:
            return True
        if not isinstance(obj, dict):
            return True
        if self.flavor == "anthropic":
            self._feed_anthropic(obj)
            return True
        return self._feed_openai(obj)

    def _feed_openai(self, obj: dict) -> bool:
        if isinstance(obj.get("model"), str):
            self.response_model = obj["model"]
        if isinstance(obj.get("error"), (dict, str)):
            self.error_type = _error_type(obj)

        choices = obj.get("choices")
        if isinstance(choices, list):
            for c in choices:
                if not isinstance(c, dict):
                    continue
                if c.get("finish_reason"):
                    self.finish_reason = c["finish_reason"]
                delta = c.get("delta")
                if not isinstance(delta, dict):
                    continue
                if isinstance(delta.get("content"), str):
                    self._text.append(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") if isinstance(tc, dict) else None
                    if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                        self._text.append(fn["arguments"])

        usage = parse_usage(obj.get("usage"), "openai")
        if usage is not None:
            self.usage = usage

        # Responses API events.
        resp = obj.get("response")
        if isinstance(resp, dict):
            if isinstance(resp.get("model"), str):
                self.response_model = resp["model"]
            ru = parse_usage(resp.get("usage"), "openai")
            if ru is not None:
                self.usage = ru
            if isinstance(resp.get("status"), str):
                self.finish_reason = resp["status"]
        if obj.get("type") == "response.output_text.delta" and isinstance(obj.get("delta"), str):
            self._text.append(obj["delta"])

        if self.strip_usage_only_chunks and usage is not None and choices == []:
            return False
        return True

    def _feed_anthropic(self, obj: dict) -> None:
        t = obj.get("type")
        if t == "message_start":
            msg = obj.get("message") or {}
            if isinstance(msg.get("model"), str):
                self.response_model = msg["model"]
            if isinstance(msg.get("usage"), dict):
                self._anthropic_usage = dict(msg["usage"])
        elif t == "message_delta":
            u = obj.get("usage")
            if isinstance(u, dict):
                # message_delta carries cumulative counts, so they override message_start.
                for k, v in u.items():
                    if v is not None:
                        self._anthropic_usage[k] = v
            delta = obj.get("delta") or {}
            if isinstance(delta, dict) and delta.get("stop_reason"):
                self.finish_reason = delta["stop_reason"]
        elif t == "content_block_delta":
            d = obj.get("delta") or {}
            if isinstance(d, dict):
                for key in ("text", "partial_json", "thinking"):
                    if isinstance(d.get(key), str):
                        self._text.append(d[key])
        elif t == "error":
            self.error_type = _error_type(obj)

        if self._anthropic_usage:
            self.usage = parse_usage(self._anthropic_usage, "anthropic")
