"""Find shell commands that print whole files into the conversation (cat, Get-Content).

The analyzer showed that most large shell outputs are file contents printed with cat,
which bypasses the Read guard. Ranged prints (sed -n 'A,Bp', head, tail) are targeted
reads and are left alone, like Read with offset/limit. So are prints that are piped
into another command or redirected to a file, because their output is not the file.

Parsing is deliberately simple. Anything it does not understand is ignored.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_SPLIT = re.compile(r"(\|\||&&|;|\n|\|)")
_STDOUT_REDIRECT = re.compile(r"(?<![0-9])>")
_CD = {"cd", "pushd", "set-location", "sl", "chdir"}
_BASH_PRINTERS = {"cat", "/bin/cat", "/usr/bin/cat"}
_PS_PRINTERS = {"get-content", "gc", "cat", "type"}
_PS_LIMITS = {"-head", "-tail", "-totalcount", "-first", "-last", "-wait"}
_PS_VALUE_FLAGS = {"-encoding", "-delimiter", "-readcount", "-stream", "-filter", "-include", "-exclude"}
# Commands that only look at things: they explore, they do not change anything.
READ_ONLY = {"cat", "sed", "grep", "rg", "ls", "cd", "echo", "head", "tail", "find", "wc", "pwd", "type", "dir",
             "tree", "stat", "file", "which", "true", "printf", "sleep", "sort", "uniq", "awk", "cut", "less",
             "Get-Content", "Select-String", "Get-ChildItem", "Test-Path", "Write-Output", "Write-Host", "date",
             "for", "while", "if", "do", "then", "export", "set", "source"}
_GIT_BASH_DRIVE = re.compile(r"^/([A-Za-z])(/|$)")


def shell_path(text: str, cwd: str | None) -> Path:
    """Resolve a path as the shell would: ~, Git Bash drives (/c/...), relative to cwd."""
    text = _GIT_BASH_DRIVE.sub(lambda m: f"{m.group(1).upper()}:/", text)
    path = Path(text).expanduser()
    return path if path.is_absolute() or not cwd else Path(cwd) / path


def _words(segment: str, powershell: bool) -> list[str]:
    try:
        words = shlex.split(segment, posix=not powershell)
    except ValueError:
        return []
    return [w.strip("'\"") for w in words] if powershell else words


def _printed_args(name: str, args: list[str], powershell: bool) -> list[str]:
    if powershell:
        if name not in _PS_PRINTERS or any(a.lower() in _PS_LIMITS for a in args):
            return []
        files, skip = [], False
        for arg in args:
            if skip:
                skip = False
            elif arg.startswith("-"):
                skip = arg.lower() in _PS_VALUE_FLAGS
            else:
                files.append(arg)
        return files
    if name not in _BASH_PRINTERS:
        return []
    return [a for a in args if not (a.startswith("-") and len(a) > 1)]


def file_prints(command: str, cwd: str | None, powershell: bool = False) -> list[Path]:
    """Existing files that `command` prints whole to its output."""
    found: list[Path] = []
    parts = _SPLIT.split(command)
    here = cwd
    for i in range(0, len(parts), 2):
        segment = parts[i].strip()
        words = _words(segment, powershell)
        if not words:
            continue
        name, args = words[0].lower() if powershell else words[0], words[1:]
        if name.lower() in _CD:
            if args and not any(ch in args[-1] for ch in "$*?`"):
                here = str(shell_path(args[-1], here))
            continue
        piped = i + 1 < len(parts) and parts[i + 1] == "|"
        if piped or "<<" in segment or _STDOUT_REDIRECT.search(segment):
            continue
        for arg in _printed_args(name, args, powershell):
            if any(ch in arg for ch in "$*?`{"):
                continue
            path = shell_path(arg, here)
            if path.is_file() and path not in found:
                found.append(path)
    return found


def is_exploration(command: str, powershell: bool = False) -> bool:
    """True when every part of the command only looks at things (cd, grep, cat, ls, ...)."""
    programs = []
    for i, part in enumerate(_SPLIT.split(command)):
        if i % 2:
            continue
        words = _words(part.strip(), powershell)
        if words:
            program = words[0].rsplit("/", 1)[-1]
            if program == "sed" and any(w.startswith("-i") for w in words[1:]):
                return False  # in-place edit
            programs.append(program)
    return bool(programs) and all(p in READ_ONLY for p in programs)
