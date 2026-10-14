"""Turn parsed timelines into a token usage report.

Two measures are used for each piece of context:
  size:    estimated tokens when it entered the context
  carried: size x number of later API calls that re-sent it. This is what it
           really cost, because every call re-reads the whole context.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .transcripts import SHELL_TOOLS, ContextItem, Timeline

LARGE_READ_TOKENS = 10_000
EXPLORE_TOOLS = {"Read", "Glob", "Grep", "Bash", "PowerShell", "Agent", "Task"}


@dataclass
class _Agg:
    count: int = 0
    tokens: float = 0
    carried: float = 0

    def add(self, item: ContextItem, scale: float) -> None:
        self.count += 1
        self.tokens += item.tokens * scale
        self.carried += item.carried_tokens * scale

    def as_dict(self) -> dict[str, Any]:
        return {"count": self.count, "tokens": round(self.tokens), "carried_tokens": round(self.carried)}


def _pct(values: list[int], p: float) -> int | None:
    if not values:
        return None
    vals = sorted(values)
    return vals[min(int(round((len(vals) - 1) * p)), len(vals) - 1)]


def calibration_factor(timelines: Iterable[Timeline]) -> tuple[float, int]:
    """Ratio of real context growth to our estimate, from calls separated only by tool output.

    For consecutive calls i-1 and i in the same context segment:
        real growth = context(i) - context(i-1) - output(i-1)
        estimate    = estimated tokens of the tool outputs added in between
    The median ratio corrects the o200k estimate towards the Claude tokenizer.
    """
    ratios: list[float] = []
    for tl in timelines:
        compaction_points = {c.position for c in tl.compactions}
        added: dict[int, list[ContextItem]] = defaultdict(list)
        for item in tl.items:
            added[item.start].append(item)
        for i in range(1, len(tl.calls)):
            if i in compaction_points:
                continue
            items = added.get(i, [])
            if not items or any(it.kind != "tool" or it.images for it in items):
                continue
            estimate = sum(it.tokens for it in items)
            if estimate < 300:
                continue
            growth = tl.calls[i].context - tl.calls[i - 1].context - tl.calls[i - 1].output
            ratio = growth / estimate
            if 0.3 <= ratio <= 4:
                ratios.append(ratio)
    if len(ratios) < 20:
        return 1.0, len(ratios)
    return round(statistics.median(ratios), 3), len(ratios)


def simulate_compaction(timelines: Iterable[Timeline], threshold: int, post_tokens: int) -> dict[str, Any]:
    """Estimate context tokens if auto-compaction had triggered at `threshold`.

    Replays every timeline: once the simulated context passes the threshold, the
    surplus is dropped and the context restarts at `post_tokens`. Each simulated
    compaction is charged one full read of the context plus the summary it writes.
    Real compactions in the data reset the simulation.
    """
    real = simulated = 0
    compactions = 0
    for tl in timelines:
        offset = 0
        previous = 0
        for call in tl.calls:
            ctx = call.context
            if ctx < previous * 0.5:  # a real compaction happened here
                offset = 0
            previous = ctx
            if ctx - offset > threshold:
                compactions += 1
                simulated += (ctx - offset) + post_tokens  # summarization call
                offset = ctx - post_tokens
            real += ctx
            simulated += ctx - offset
    return {
        "threshold": threshold,
        "real_tokens": real,
        "simulated_tokens": simulated,
        "saving_share": 1 - simulated / real if real else 0,
        "extra_compactions": compactions,
    }


def _redundant_reads(tl: Timeline) -> list[ContextItem]:
    """Full re-reads of the same file and range with no edit in between, in one context segment."""
    edits_by_path: dict[str, list[int]] = defaultdict(list)
    for pos, path in tl.edits:
        edits_by_path[path].append(pos)
    last_read: dict[tuple, ContextItem] = {}
    redundant: list[ContextItem] = []
    for item in tl.items:
        if item.kind != "tool" or item.name != "Read" or not item.path or item.result_type != "text":
            continue
        key = (item.path, item.read_range)
        prev = last_read.get(key)
        # The earlier read must still be in context (no compaction since) and the file unedited.
        if prev is not None and prev.end > item.start:
            edited = any(prev.start <= pos <= item.start for pos in edits_by_path.get(item.path, []))
            if not edited:
                redundant.append(item)
        last_read[key] = item
    return redundant


def analyze(
    timelines: list[Timeline], *, top: int = 15, estimate_method: str = "", calibration: float | None = None
) -> dict[str, Any]:
    # A fixed calibration keeps small samples (one session) comparable with large ones.
    scale, samples = (calibration, 0) if calibration else calibration_factor(timelines)

    models: dict[str, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    sub_totals: Counter = Counter()
    calls_by_model: dict[str, Counter] = {"main": Counter(), "subagent": Counter()}
    contexts: list[int] = []
    startup: list[int] = []
    orientation: list[float] = []
    sources: dict[str, _Agg] = defaultdict(_Agg)
    tools: dict[str, _Agg] = defaultdict(_Agg)
    shell_groups: dict[str, _Agg] = defaultdict(_Agg)
    read_files: dict[str, _Agg] = defaultdict(_Agg)
    tool_items: list[tuple[ContextItem, Timeline]] = []
    redundant: list[tuple[ContextItem, Timeline]] = []
    large_reads: list[tuple[ContextItem, Timeline]] = []
    read_types: Counter = Counter()
    compactions = Counter()
    pre_compact: list[int] = []
    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    fixed = _Agg()
    outputs = _Agg()
    real_context = 0
    thinking_total = 0
    output_parts: dict[str, _Agg] = defaultdict(_Agg)

    for tl in timelines:
        for call in tl.calls:
            for key, value in (
                ("calls", 1),
                ("input", call.input),
                ("cache_creation", call.cache_creation),
                ("cache_read", call.cache_read),
                ("output", call.output),
            ):
                models[call.model][key] += value
                totals[key] += value
                if tl.is_subagent:
                    sub_totals[key] += value
            contexts.append(call.context)
            calls_by_model["subagent" if tl.is_subagent else "main"][call.model] += 1
        if tl.calls and not tl.is_subagent:
            startup.append(tl.calls[0].context)
            # Exploration before the first edit: what a project map could replace.
            first_edit = min((pos for pos, _ in tl.edits), default=len(tl.calls))
            orientation.append(scale * sum(
                it.tokens for it in tl.items
                if it.kind == "tool" and it.name in EXPLORE_TOOLS and it.start <= first_edit
            ))
        real_context += sum(c.context for c in tl.calls)

        # Fixed load: what the first call saw beyond the items we can attribute.
        # System prompt and tool definitions survive compaction, so every call carries it.
        if tl.calls:
            first_items = sum(it.tokens for it in tl.items if it.start == 0) * scale
            base = max(tl.calls[0].context - first_items, 0)
            fixed.count += 1
            fixed.tokens += base
            fixed.carried += base * len(tl.calls)

        # Visible model output (text, tool calls) stays in context until the next compaction.
        # Thinking is counted as output but treated as not carried: it is mostly dropped later.
        boundaries = sorted(c.position for c in tl.compactions)
        for call in tl.calls:
            seg_end = next((b for b in boundaries if b > call.index), len(tl.calls))
            estimate = call.visible_output_est
            visible = min(call.output, round(estimate * scale))
            carried_calls = max(seg_end - call.index - 1, 0)
            outputs.count += 1
            outputs.tokens += visible
            outputs.carried += visible * carried_calls
            thinking_total += call.output - visible
            if estimate:
                for kind, n in call.visible_parts.items():
                    share = visible * n / estimate
                    agg = output_parts["Metin" if kind == "text" else f"{kind} çağrısı"]
                    agg.count += 1
                    agg.tokens += share
                    agg.carried += share * carried_calls

        for c in tl.compactions:
            compactions[c.trigger or "?"] += 1
            if c.pre_tokens:
                pre_compact.append(int(c.pre_tokens))

        for item in tl.items:
            if item.kind == "tool":
                sources[f"tool:{item.name}"].add(item, scale)
                tools[item.name].add(item, scale)
                tool_items.append((item, tl))
                if item.name in SHELL_TOOLS:
                    shell_groups[item.group or "?"].add(item, scale)
                if item.name == "Read":
                    read_types[item.result_type or "?"] += 1
                    if item.path:
                        read_files[item.descriptor.split(" [")[0]].add(item, scale)
                    if item.tokens * scale >= LARGE_READ_TOKENS and item.read_range == (None, None):
                        large_reads.append((item, tl))
            elif item.kind == "attachment":
                sources[f"attachment:{item.name}"].add(item, scale)
            else:
                sources[item.kind].add(item, scale)
        redundant.extend((it, tl) for it in _redundant_reads(tl))

        key = (tl.project, tl.session_id)
        s = sessions.setdefault(
            key,
            {"project": tl.project, "session_id": tl.session_id, "title": None, "calls": 0, "tokens": 0,
             "compactions": 0, "max_context": 0, "subagents": 0, "first_ts": tl.first_ts, "last_ts": tl.last_ts},
        )
        if not tl.is_subagent:
            s["title"] = tl.title
        s["subagents"] += int(tl.is_subagent)
        s["calls"] += len(tl.calls)
        s["tokens"] += sum(c.total for c in tl.calls)
        s["compactions"] += len(tl.compactions)
        s["max_context"] = max([s["max_context"], *(c.context for c in tl.calls)])
        s["first_ts"] = min(filter(None, [s["first_ts"], tl.first_ts]), default=None)
        s["last_ts"] = max(filter(None, [s["last_ts"], tl.last_ts]), default=None)

    sources["fixed"] = fixed
    sources["model_output"] = outputs
    processed = totals["input"] + totals["cache_creation"] + totals["cache_read"] + totals["output"]
    carried_all = sum(a.carried for a in sources.values())

    def top_items(pairs: list[tuple[ContextItem, Timeline]], n: int) -> list[dict[str, Any]]:
        ranked = sorted(pairs, key=lambda p: -p[0].carried_tokens)[:n]
        return [
            {
                "tool": it.name,
                "target": it.descriptor,
                "tokens": round(it.tokens * scale),
                "carried_calls": it.carried_calls,
                "carried_tokens": round(it.carried_tokens * scale),
                "images": it.images,
                "session": tl.title or tl.session_id[:8],
                "project": tl.project,
                "subagent": tl.is_subagent,
            }
            for it, tl in ranked
        ]

    def ranked(aggs: dict[str, _Agg], n: int | None = None) -> list[dict[str, Any]]:
        rows = [{"name": k, **v.as_dict()} for k, v in sorted(aggs.items(), key=lambda kv: -kv[1].carried)]
        return rows[:n] if n else rows

    post = [int(c.post_tokens) for t in timelines for c in t.compactions if c.post_tokens]
    post_tokens = int(statistics.median(post)) if post else 30_000
    what_if = [simulate_compaction(timelines, t, post_tokens) for t in (150_000, 250_000, 400_000, 600_000)]

    redundant_tokens = sum(it.tokens for it, _ in redundant) * scale
    redundant_carried = sum(it.carried_tokens for it, _ in redundant) * scale

    return {
        "scope": {
            "timelines": len(timelines),
            "sessions": len(sessions),
            "subagent_timelines": sum(1 for t in timelines if t.is_subagent),
            "projects": len({t.project for t in timelines}),
            "first": min((t.first_ts for t in timelines if t.first_ts), default=None),
            "last": max((t.last_ts for t in timelines if t.last_ts), default=None),
        },
        "tokens": {
            "calls": totals["calls"],
            "input": totals["input"],
            "cache_creation": totals["cache_creation"],
            "cache_read": totals["cache_read"],
            "output": totals["output"],
            "thinking_est": thinking_total,
            "processed": processed,
            "cache_read_share": totals["cache_read"] / processed if processed else 0,
            "subagent_share": (sum(sub_totals[k] for k in ("input", "cache_creation", "cache_read", "output")) / processed)
            if processed else 0,
            "calls_by_model": {side: dict(c.most_common()) for side, c in calls_by_model.items()},
            "by_model": [
                {"model": m, **dict(c)} for m, c in sorted(models.items(), key=lambda kv: -kv[1]["calls"])
            ],
        },
        "context": {
            "per_call_p50": _pct(contexts, 0.5),
            "per_call_p95": _pct(contexts, 0.95),
            "max": max(contexts, default=None),
            "startup_p50": _pct(startup, 0.5),
            "orientation_p50": _pct(orientation, 0.5),
            "startup_max": max(startup, default=None),
            "compactions": dict(compactions),
            "pre_compact_p50": _pct(pre_compact, 0.5),
            "post_compact_p50": post_tokens,
            "what_if_compaction": what_if,
        },
        "estimation": {
            "method": estimate_method,
            "calibration": scale,
            "calibration_samples": samples,
            "explained_share": carried_all / real_context if real_context else 0,
        },
        "sources": [
            {**row, "carried_share": row["carried_tokens"] / carried_all if carried_all else 0}
            for row in ranked(sources)
        ],
        "model_output_parts": ranked(output_parts, 8),
        "tools": ranked(tools),
        "top_outputs": top_items(tool_items, top),
        "shell_groups": ranked(shell_groups, top),
        "reads": {
            "by_type": dict(read_types),
            "redundant": {
                "count": len(redundant),
                "tokens": round(redundant_tokens),
                "carried_tokens": round(redundant_carried),
                "examples": top_items(redundant, 5),
            },
            "large_full_reads": {"count": len(large_reads), "examples": top_items(large_reads, 5)},
            "most_read_files": sorted(
                ({"name": k, **v.as_dict()} for k, v in read_files.items() if v.count > 1),
                key=lambda r: -r["carried_tokens"],
            )[:top],
        },
        "sessions": sorted(sessions.values(), key=lambda s: -s["tokens"])[:top],
    }
