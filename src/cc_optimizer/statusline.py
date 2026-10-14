"""Status line: model, context size against the warning threshold, session cost."""

from __future__ import annotations

import json
from typing import Any

from .guard_config import load_config
from .hooks import _k, last_context_tokens

_GREEN, _YELLOW, _RED, _RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def render(data: dict[str, Any], cfg: dict[str, Any]) -> str:
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    window = data.get("context_window") if isinstance(data.get("context_window"), dict) else {}
    size = window.get("context_window_size")
    usage = window.get("current_usage") if isinstance(window.get("current_usage"), dict) else {}
    used = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if not used:
        used = last_context_tokens(data.get("transcript_path")) or 0

    parts: list[str] = []
    name = model.get("display_name") or model.get("id")
    if name:
        parts.append(str(name))
    if used:
        warn, step = int(cfg["context_warn_tokens"]), int(cfg["context_step_tokens"])
        color = _GREEN if used < warn else _YELLOW if used < warn + 2 * step else _RED
        text = f"bağlam {_k(used)}"
        if isinstance(size, (int, float)) and size:
            text += f"/{_k(int(size))} (%{100 * used / size:.0f})"
        parts.append(f"{color}{text}{_RESET}")
    cost = (data.get("cost") or {}).get("total_cost_usd") if isinstance(data.get("cost"), dict) else None
    if isinstance(cost, (int, float)) and cost > 0:
        parts.append(f"${cost:.2f}")
    return " · ".join(parts)


def run(stdin_text: str) -> str:
    try:
        data = json.loads(stdin_text) if stdin_text.strip() else {}
        return render(data if isinstance(data, dict) else {}, load_config())
    except Exception:
        return ""
