"""Two interface languages, English and Turkish. Every user-facing message is written next to its translation:

    L("Guards installed", "Korumalar kuruldu")

The language is, in order: the CC_OPTIMIZER_LANG environment variable ("en" or "tr"), the `language` setting in
config.json ("en", "tr" or "auto"), the system language (Turkish when it is Turkish), then English. Text that goes
to Claude (hook guidance, subagent prompts) is always English and does not use this module.
"""

from __future__ import annotations

import os
import sys

LANGUAGES = ("en", "tr")
_cache: dict[tuple, str] = {}


def _system_language() -> str:
    names = [os.environ.get(v, "") for v in ("LC_ALL", "LC_MESSAGES", "LANG")]
    if sys.platform == "win32":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
                names.insert(0, buf.value)
        except (AttributeError, OSError):
            pass
    first = next((n for n in names if n), "")
    return "tr" if first.lower().startswith("tr") else "en"


def language() -> str:

    key = (os.environ.get("CC_OPTIMIZER_LANG"), os.environ.get("CC_OPTIMIZER_HOME"))
    if key not in _cache:
        chosen = (key[0] or "").strip().lower()
        if chosen not in LANGUAGES:
            from .guard_config import load_config

            chosen = str(load_config().get("language", "auto")).strip().lower()
        _cache[key] = chosen if chosen in LANGUAGES else _system_language()
    return _cache[key]


def reset() -> None:
    _cache.clear()


def plural(n: int, one: str, many: str | None = None) -> str:
    """English "1 session" / "2 sessions". Turkish has no plural after a number, so callers use it for the English side."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


def decimal(text: str) -> str:
    """Turn a number formatted the English way (1,234.5) into the active language's way (Turkish: 1.234,5)."""
    return text.replace(",", "_").replace(".", ",").replace("_", ".") if language() == "tr" else text


def L(en: str, tr: str) -> str:
    return tr if language() == "tr" else en
