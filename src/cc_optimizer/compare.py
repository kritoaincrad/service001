"""Before/after comparison of two analyze reports.

Sessions before and after the guards were installed are different pieces of work, so
totals are not comparable. Every metric is normalized: per call, a median or a share.

When per-session values are available (the token history has them), each row also gets a
confidence: a bootstrap over sessions says whether the change is larger than the session-to-session
noise. A change that cannot be told apart from noise is shown as "unclear", not as better or worse.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Callable
from typing import Any

from .i18n import L, language, plural

MIN_SESSIONS = 5
MIN_CALLS = 1000
MIN_SESSIONS_FOR_CONFIDENCE = 3  # per side; with fewer sessions a bootstrap says nothing
BOOTSTRAP_ROUNDS = 1000
CONFIDENCE_LEVEL = 0.90
MIN_PROJECT_OVERLAP = 0.5
CHEAP_MODELS = ("haiku", "sonnet")


def _per_call(value: float | None, report: dict[str, Any]) -> float | None:
    calls = report["tokens"]["calls"]
    return value / calls if value is not None and calls else None


def _carried(name: str) -> Callable[[dict[str, Any]], float | None]:
    def get(report: dict[str, Any]) -> float | None:
        names = name.split("+")
        rows = [s for s in report.get("sources", []) if s["name"] in names]
        return sum(s["carried_share"] for s in rows) if rows else 0.0
    return get


def _cheap_share(side: str) -> Callable[[dict[str, Any]], float | None]:
    def get(report: dict[str, Any]) -> float | None:
        by_model = report["tokens"].get("calls_by_model", {}).get(side)
        if not by_model:
            return None  # older report without this field, or no such calls
        cheap = sum(n for model, n in by_model.items() if any(m in model for m in CHEAP_MODELS))
        return cheap / sum(by_model.values())
    return get


def metrics() -> list[tuple[str, Callable[[dict[str, Any]], float | None], str, bool | None]]:
    """(label, getter, format, lower is better; None when neither direction is better by itself).

    A function, not a constant: the labels follow the language at call time.
    """
    return [
        (L("Tokens processed per call", "Çağrı başına işlenen token"), lambda r: _per_call(r["tokens"]["processed"], r), "tokens", True),
        (L("Context per call, median", "Çağrı başına bağlam, medyan"), lambda r: r["context"]["per_call_p50"], "tokens", True),
        (L("Context before compaction, median", "Sıkıştırma öncesi bağlam, medyan"), lambda r: r["context"]["pre_compact_p50"], "tokens", True),
        (L("Fixed load at session start, median", "Oturum açılışındaki sabit yük, medyan"), lambda r: r["context"]["startup_p50"], "tokens", True),
        (L("Exploration before the first edit, median", "İlk değişiklikten önceki keşif, medyan"), lambda r: r["context"].get("orientation_p50"), "tokens", True),
        (L("Unlimited large reads, per 1000 calls", "Sınırsız büyük okuma, 1000 çağrıda"), lambda r: _per_call(1000 * r["reads"]["large_full_reads"]["count"], r), "number", True),
        (L("File reads share of load", "Yükte dosya okumalarının payı"), _carried("tool:Read"), "share", True),
        (L("Shell output share of load", "Yükte kabuk çıktılarının payı"), _carried("tool:Bash+tool:PowerShell"), "share", True),
        (L("Model output share of load", "Yükte modelin yazdıklarının payı"), _carried("model_output"), "share", True),
        (L("Subagent share of usage", "Alt ajanların tüketim payı"), lambda r: r["tokens"]["subagent_share"], "share", None),
        (L("Haiku/Sonnet call share in the main chat", "Ana sohbette Haiku/Sonnet çağrı payı"), _cheap_share("main"), "share", False),
        (L("Haiku/Sonnet call share in subagents", "Alt ajanlarda Haiku/Sonnet çağrı payı"), _cheap_share("subagent"), "share", False),
    ]


METRIC_KEYS = (
    "tokens_per_call", "context_per_call_p50", "context_pre_compact_p50", "startup_load_p50", "orientation_p50",
    "large_full_reads_per_1000", "share_file_reads", "share_shell_output", "share_model_output", "share_subagents",
    "cheap_model_share_main", "cheap_model_share_subagents",
)  # same order as metrics(): language-independent ids for the JSON output

# Per-session values for one metric: a float (the side's value is the median over sessions) or a
# (numerator, denominator) pair (the side's value is sum(numerator) / sum(denominator), like the headline).
Sample = float | tuple[float, float]


def _statistic(items: list[Sample]) -> float | None:
    if not items:
        return None
    if isinstance(items[0], tuple):
        den = sum(i[1] for i in items)
        return sum(i[0] for i in items) / den if den else None
    return statistics.median(items)


def _bootstrap(before: list[Sample], after: list[Sample], fmt: str) -> tuple[float, float] | None:
    """Percentile interval of the change (relative, or in share points) when sessions are resampled."""
    rng = random.Random(20261015)  # fixed seed: the same data always gives the same report
    changes = []
    for _ in range(BOOTSTRAP_ROUNDS):
        sb = _statistic([rng.choice(before) for _ in before])
        sa = _statistic([rng.choice(after) for _ in after])
        if sb is None or sa is None:
            continue
        if fmt == "share":
            changes.append(sa - sb)
        elif sb:
            changes.append((sa - sb) / sb)
    if len(changes) < BOOTSTRAP_ROUNDS * 0.8:
        return None
    changes.sort()
    tail = (1 - CONFIDENCE_LEVEL) / 2
    return changes[int(tail * len(changes))], changes[int((1 - tail) * len(changes)) - 1]


def _confidence(samples: tuple[list[Sample], list[Sample]] | None, fmt: str) -> tuple[str | None, list[float] | None]:
    """("clear" | "unclear" | "insufficient" | None when there are no per-session values, interval)."""
    if samples is None:
        return None, None
    before, after = ([s for s in side if s is not None] for side in samples)
    if min(len(before), len(after)) < MIN_SESSIONS_FOR_CONFIDENCE:
        return "insufficient", None
    interval = _bootstrap(before, after, fmt)
    if interval is None:
        return "insufficient", None
    low, high = interval
    return ("clear" if low > 0 or high < 0 else "unclear"), [low, high]


def compare(before: dict[str, Any], after: dict[str, Any],
            samples: dict[str, tuple[list[Sample], list[Sample]]] | None = None) -> dict[str, Any]:
    rows = []
    for key, (label, get, fmt, lower_better) in zip(METRIC_KEYS, metrics(), strict=True):
        try:
            b, a = get(before), get(after)
        except (KeyError, TypeError, ZeroDivisionError):
            b = a = None
        change = None
        if b is not None and a is not None:
            change = (a - b) if fmt == "share" else ((a - b) / b if b else None)
        better = None if change is None or change == 0 or lower_better is None else (change < 0) == lower_better
        confidence, interval = _confidence(samples.get(key) if samples else None, fmt) if change is not None else (None, None)
        rows.append({"key": key, "metric": label, "format": fmt, "before": b, "after": a, "change": change,
                     "better": better, "confidence": confidence, "interval": interval})
    small = after["scope"]["sessions"] < MIN_SESSIONS or after["tokens"]["calls"] < MIN_CALLS
    return {
        "before": {k: before["scope"].get(k) for k in ("sessions", "first", "last")} | {"calls": before["tokens"]["calls"]},
        "after": {k: after["scope"].get(k) for k in ("sessions", "first", "last")} | {"calls": after["tokens"]["calls"]},
        "small_sample": small,
        "rows": rows,
    }


def _num(value: float, digits: int = 0) -> str:
    """Turkish number format 13.968 and 7,5; English 13,968 and 7.5."""
    text = f"{value:,.{digits}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".") if language() == "tr" else text


def _fmt(value: float | None, fmt: str) -> str:
    if value is None:
        return "-"
    if fmt == "share":
        return f"%{_num(100 * value, 1)}" if language() == "tr" else f"{_num(100 * value, 1)}%"
    if fmt == "tokens":
        return f"{_num(value / 1000)}K" if value >= 1000 else _num(value)
    return _num(value, 1)


def _amount(value: float, fmt: str) -> str:
    """A change as text: relative for most rows, share points for shares."""
    sign = "+" if value > 0 else "-"
    if fmt == "share":
        return f"{sign}{_num(abs(100 * value), 1)} " + L("pts", "puan")
    return f"{sign}%{_num(abs(100 * value))}" if language() == "tr" else f"{sign}{_num(abs(100 * value))}%"


def _verdict(row: dict[str, Any]) -> str:
    confidence = row.get("confidence")
    if confidence in ("unclear", "insufficient"):
        return L("  unclear", "  belirsiz")  # not distinguishable from session-to-session noise
    return {True: L("  better", "  iyi"), False: L("  worse", "  kötü"), None: ""}[row["better"]]


def _change(row: dict[str, Any]) -> str:
    change = row["change"]
    if change is None:
        return "-"
    if abs(change) < 0.0005:
        return L("unchanged", "değişmedi")
    return _amount(change, row["format"]) + _verdict(row)


def _interval_text(row: dict[str, Any]) -> str:
    interval = row.get("interval")
    if not interval:
        return ""
    return f"[{_amount(interval[0], row['format'])} .. {_amount(interval[1], row['format'])}]"


def _period(side: dict[str, Any]) -> str:
    span = f"{(side['first'] or '')[:10]} - {(side['last'] or '')[:10]}"
    return L(f"{plural(side['sessions'], 'session')}, {_num(side['calls'])} {'call' if side['calls'] == 1 else 'calls'} ({span})",
             f"{side['sessions']} oturum, {_num(side['calls'])} çağrı ({span})")


def _top_projects(shares: dict[str, float], limit: int = 3) -> str:
    top = sorted(shares.items(), key=lambda kv: -kv[1])[:limit]
    return ", ".join(f"{name} {round(100 * share)}%" for name, share in top) + (", ..." if len(shares) > limit else "")


def _warnings(result: dict[str, Any]) -> list[str]:
    out = []
    if result["small_sample"]:
        out.append(L(f"Warning: the later period has fewer than {MIN_SESSIONS} sessions or {_num(MIN_CALLS)} calls of data. Results may be misleading.",
                     f"Uyarı: Sonraki dönemde {MIN_SESSIONS} oturum veya {_num(MIN_CALLS)} çağrıdan az veri var. Sonuçlar yanıltıcı olabilir."))
    projects = result.get("projects")
    if projects and projects["overlap"] < MIN_PROJECT_OVERLAP and not projects.get("matched"):
        out.append(L(
            f"Warning: the two periods mostly cover different projects (overlap {round(100 * projects['overlap'])}%). "
            f"Before: {_top_projects(projects['before'])}. After: {_top_projects(projects['after'])}. "
            "Differences may come from the work, not from the guards. For a like-for-like comparison use --matched "
            "(only projects present on both sides) or --project NAME.",
            f"Uyarı: İki dönem büyük ölçüde farklı projeleri kapsıyor (örtüşme %{round(100 * projects['overlap'])}). "
            f"Önce: {_top_projects(projects['before'])}. Sonra: {_top_projects(projects['after'])}. "
            "Farklar korumalardan değil işin kendisinden gelebilir. Eşit koşullu karşılaştırma için --matched "
            "(yalnızca iki tarafta da bulunan projeler) veya --project AD kullanın."))
    if any(r.get("confidence") in ("unclear", "insufficient") for r in result["rows"]):
        out.append(L(
            f"\"unclear\": the change is within the session-to-session noise ({int(100 * CONFIDENCE_LEVEL)}% interval includes 0), "
            f"or a side has fewer than {MIN_SESSIONS_FOR_CONFIDENCE} sessions. More sessions are needed to call it better or worse.",
            f"\"belirsiz\": değişim oturumdan oturuma dalgalanmanın içinde (%{int(100 * CONFIDENCE_LEVEL)} aralık 0'ı içeriyor) "
            f"veya bir tarafta {MIN_SESSIONS_FOR_CONFIDENCE} oturumdan az var. İyi ya da kötü demek için daha çok oturum gerekir."))
    return out


def _guard_effect(tokens: dict[str, int] | None) -> list[str]:
    """What the blocking guards kept out of the context, from the event log. An upper bound: a blocked read may be
    retried in smaller parts."""
    if not tokens or not tokens.get("blocked_tokens"):
        return []
    text = L(f"- Kept out of the context at the first attempt: about {_num(tokens['blocked_tokens'] / 1000)}K tokens "
             f"({plural(tokens['blocked'], 'blocked read')}). An upper bound: a blocked read can be retried in smaller parts",
             f"- İlk denemede bağlama girmesi engellenen: yaklaşık {_num(tokens['blocked_tokens'] / 1000)}K token "
             f"({tokens['blocked']} engellenen okuma). Üst sınırdır: engellenen okuma daha küçük parçalarla yeniden denenebilir")
    if tokens.get("retried_tokens"):
        text += L(f"; {_num(tokens['retried_tokens'] / 1000)}K of it was dumped anyway after a retry.",
                  f"; bunun {_num(tokens['retried_tokens'] / 1000)}K kadarı yeniden denemeyle yine de bağlama alındı.")
    else:
        text += "."
    return [text]


def _table(result: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    return [(r["metric"], _fmt(r["before"], r["format"]), _fmt(r["after"], r["format"]), _change(r), _interval_text(r))
            for r in result["rows"]]


def _notes() -> list[str]:
    return [
        L("- The work in the two periods differs. So per-call values, medians and shares are compared, not totals.",
          "- İki dönemdeki işler farklıdır. Bu yüzden toplamlar değil, çağrı başına değerler, medyanlar ve paylar karşılaştırılır."),
        L("- The main metric is tokens processed per call. The other rows show where the change comes from.",
          "- Asıl ölçüt çağrı başına işlenen token. Diğer satırlar değişimin nereden geldiğini gösterir."),
        L("- \"-\" means the metric was missing from the older report or had no data in that period.",
          "- \"-\" o ölçütün eski raporda olmadığı veya o dönemde hiç verisi olmadığı anlamına gelir."),
    ]


def render(result: dict[str, Any], events: dict[str, int] | None = None,
           labels: tuple[str, str] | None = None, title_suffix: str = "",
           guard_tokens: dict[str, int] | None = None) -> str:
    labels = labels or (L("Before", "Önce"), L("After", "Sonra"))
    pad = max(len(labels[0]), len(labels[1])) + 2
    lines = [
        L(f"Service001 before / after comparison{title_suffix}", f"Service001 önce / sonra karşılaştırması{title_suffix}"),
        "",
        f"{labels[0] + ':':<{pad}}{_period(result['before'])}",
        f"{labels[1] + ':':<{pad}}{_period(result['after'])}",
        "",
    ]
    for warning in _warnings(result):
        lines += [warning, ""]
    table = _table(result)
    width = max(len(r[0]) for r in table) + 2
    lines.append(f"{L('Metric', 'Ölçüt'):<{width}}{L('Before', 'Önce'):>10}{L('After', 'Sonra'):>10}   {L('Change', 'Değişim')}")  # columns: labels[0], labels[1]
    for metric, b, a, change, interval in table:
        lines.append(f"{metric:<{width}}{b:>10}{a:>10}   {change}" + (f"  {interval}" if interval else ""))
    if events:
        lines += ["", L("Guard events in the later period:", "Sonraki dönemde koruyucu olayları:")]
        lines += [f"- {kind}: {count}" for kind, count in sorted(events.items(), key=lambda kv: -kv[1])]
    lines += _guard_effect(guard_tokens)
    return "\n".join(lines + ["", L("Notes:", "Notlar:"), *_notes()])


def render_markdown(result: dict[str, Any], events: dict[str, int] | None = None,
                    labels: tuple[str, str] | None = None, title_suffix: str = "",
                    guard_tokens: dict[str, int] | None = None) -> str:
    """The same report as a Markdown table, to paste into a pull request, a ticket or a team update."""
    labels = labels or (L("Before", "Önce"), L("After", "Sonra"))
    lines = [
        "## " + L(f"Service001 before / after comparison{title_suffix}", f"Service001 önce / sonra karşılaştırması{title_suffix}"),
        "",
        f"- **{labels[0]}:** {_period(result['before'])}",
        f"- **{labels[1]}:** {_period(result['after'])}",
        "",
    ]
    for warning in _warnings(result):
        lines += [f"> {warning}", ""]
    lines += [f"| {L('Metric', 'Ölçüt')} | {labels[0]} | {labels[1]} | {L('Change', 'Değişim')} | "
              f"{int(100 * CONFIDENCE_LEVEL)}% {L('interval', 'aralık')} |", "|---|--:|--:|---|---|"]
    lines += [f"| {m} | {b} | {a} | {c.strip()} | {i} |" for m, b, a, c, i in _table(result)]
    if events:
        lines += ["", "**" + L("Guard events in the later period", "Sonraki dönemde koruyucu olayları") + "**", ""]
        lines += [f"- {kind}: {count}" for kind, count in sorted(events.items(), key=lambda kv: -kv[1])]
    effect = _guard_effect(guard_tokens)
    if effect:
        lines += ["", *effect]
    return "\n".join(lines + ["", *(n.replace("- ", "- _", 1) + "_" for n in _notes())]) + "\n"
