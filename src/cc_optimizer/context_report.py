"""Where does the fixed load at the start of a session come from?

Claude Code records everything it adds before the first model call as attachments in the transcript
(skill list, subagent list, MCP instructions, hook context, ...). This sizes each of them and sets the
total against what the first call really processed. The remainder is the Claude Code base prompt and
tool definitions, which no setting changes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .i18n import L
from .transcripts import NON_CONTEXT_ATTACHMENTS, TokenCounter, _attachment_text

# Attachment type -> (English label, Turkish label, who controls it)
SOURCES: dict[str, tuple[str, str, str]] = {
    "skill_listing": ("Skill list", "Skill listesi", "account"),
    "agent_listing_delta": ("Subagent list", "Alt ajan listesi", "claude"),
    "mcp_instructions_delta": ("MCP instructions", "MCP talimatları", "account"),
    "deferred_tools_delta": ("Deferred tool names", "Ertelenmiş araç adları", "claude"),
    "hook_additional_context": ("Hook context", "Hook bağlamı", "hooks"),
    "environment": ("Environment", "Ortam bilgisi", "claude"),
    "session_context": ("Session context", "Oturum bağlamı", "claude"),
    "date": ("Date", "Tarih", "claude"),
    "model": ("Model note", "Model notu", "claude"),
}
# Echo of hook output for the interface; the same text is already counted as hook context.
IGNORED = NON_CONTEXT_ATTACHMENTS | {"hook_success"}
OWNERS = {
    "claude": ("Claude Code", "Claude Code"),
    "account": ("your account or plugins", "hesabınız veya eklentileriniz"),
    "hooks": ("hooks", "hook'lar"),
    "service001": ("Service001", "Service001"),
}
OUR_AGENTS = ("architect", "expert", "scout", "worker")


def latest_transcript(root: Path, project: str | None = None, session: str | None = None) -> Path | None:
    """The most recently written main transcript. `project` matches part of the project folder name."""
    from .transcripts import find_transcripts

    needle = re.sub(r"[^a-z0-9]", "-", project.lower()) if project else None
    mains = [p for p, name, _, sub in find_transcripts(root, None, session, include_subagents=False)
             if not sub and (not needle or needle in name.lower())]
    return max(mains, key=lambda p: p.stat().st_mtime, default=None)


def _split_agents(text: str) -> tuple[str, str]:
    """Split the subagent list into our own agents and the rest."""
    ours, rest = [], []
    for block in re.split(r"\n(?=- )", text):
        (ours if re.match(rf"- ({'|'.join(OUR_AGENTS)})\b", block) else rest).append(block)
    return "\n".join(ours), "\n".join(rest)


def measure(transcript: Path, counter: TokenCounter | None = None) -> dict[str, Any] | None:
    """Sizes of what was added before the first main-chat model call. None when there is no call."""
    counter = counter or TokenCounter()
    sizes: dict[str, list[str]] = {}
    first_total = None
    with transcript.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "assistant" and not rec.get("isSidechain"):
                usage = (rec.get("message") or {}).get("usage") or {}
                first_total = sum(usage.get(k) or 0 for k in
                                  ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                break
            att = rec.get("attachment")
            if isinstance(att, dict) and att.get("type") not in IGNORED:
                texts: list[str] = []
                _attachment_text({k: v for k, v in att.items() if k not in ("type", "hookName", "hookEvent",
                                                                               "toolUseID")}, texts)
                sizes.setdefault(str(att.get("type")), []).append("".join(texts))
    if first_total is None:
        return None

    rows: list[dict[str, Any]] = []
    for kind, parts in sizes.items():
        text = "\n".join(parts)
        en, tr, owner = SOURCES.get(kind, (kind, kind, "claude"))
        if kind == "agent_listing_delta":
            ours, rest = _split_agents(text)
            if ours:
                rows.append({"kind": "our_agents", "en": "Service001 subagents", "tr": "Service001 alt ajanları",
                             "owner": "service001", "tokens": counter.count(ours)})
            text = rest
        rows.append({"kind": kind, "en": en, "tr": tr, "owner": owner, "tokens": counter.count(text)})
    for row in rows:
        if row["kind"] == "hook_additional_context":
            row["owner"] = "service001"  # the only hooks installed by this tool; others are unknown
    rows.sort(key=lambda r: -r["tokens"])
    counted = sum(r["tokens"] for r in rows)
    return {"first_call": first_total, "rows": rows, "counted": counted, "remainder": max(0, first_total - counted),
            "method": counter.method, "transcript": str(transcript)}


def render(result: dict[str, Any] | None) -> str:
    if not result:
        return L("No session with a model call found.", "Model çağrısı olan bir oturum bulunamadı.")

    def k(n: int) -> str:
        return f"{n / 1000:.1f}K"

    lines = [L("Fixed load at session start", "Oturum açılışındaki sabit yük"),
             L(f"First model call processed {k(result['first_call'])} tokens. Sizes below are estimates "
               f"({result['method']}).",
               f"İlk model çağrısı {k(result['first_call'])} token işledi. Aşağıdaki boyutlar tahmindir "
               f"({result['method']})."), ""]
    width = max([len(L(r["en"], r["tr"])) for r in result["rows"]] + [34])
    for r in (r for r in result["rows"] if r["tokens"] >= 50):  # smaller items would print as 0.0K
        owner = OWNERS[r["owner"]]
        lines.append(f"{L(r['en'], r['tr']):<{width}} {k(r['tokens']):>7}   {L(*owner)}")
    lines.append(f"{L('Base prompt, tool definitions (not itemised)', 'Taban prompt, araç tanımları (ayrılamayan)'):<{width}} "
                 f"{k(result['remainder']):>7}   Claude Code")
    ours = sum(r["tokens"] for r in result["rows"] if r["owner"] == "service001")
    lines += ["", L(f"Added by Service001: about {k(ours)} of {k(result['first_call'])} "
                    f"({100 * ours / max(1, result['first_call']):.1f}%).",
                    f"Service001'in eklediği: {k(result['first_call'])} içinden yaklaşık {k(ours)} "
                    f"(%{100 * ours / max(1, result['first_call']):.1f}).")]
    return "\n".join(lines)
