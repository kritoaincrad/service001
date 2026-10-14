"""Before/after comparison of two analyze reports.

Sessions before and after the guards were installed are different pieces of work, so
totals are not comparable. Every metric is normalized: per call, a median or a share.
"""

from __future__ import annotations

from typing import Any, Callable

MIN_SESSIONS = 5
MIN_CALLS = 1000
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


# (label, getter, format, lower is better; None when neither direction is better by itself)
METRICS: list[tuple[str, Callable[[dict[str, Any]], float | None], str, bool | None]] = [
    ("Çağrı başına işlenen token", lambda r: _per_call(r["tokens"]["processed"], r), "tokens", True),
    ("Çağrı başına bağlam, medyan", lambda r: r["context"]["per_call_p50"], "tokens", True),
    ("Sıkıştırma öncesi bağlam, medyan", lambda r: r["context"]["pre_compact_p50"], "tokens", True),
    ("Oturum açılışındaki sabit yük, medyan", lambda r: r["context"]["startup_p50"], "tokens", True),
    ("İlk değişiklikten önceki keşif, medyan", lambda r: r["context"].get("orientation_p50"), "tokens", True),
    ("Sınırsız büyük okuma, 1000 çağrıda", lambda r: _per_call(1000 * r["reads"]["large_full_reads"]["count"], r), "number", True),
    ("Yükte dosya okumalarının payı", _carried("tool:Read"), "share", True),
    ("Yükte kabuk çıktılarının payı", _carried("tool:Bash+tool:PowerShell"), "share", True),
    ("Yükte modelin yazdıklarının payı", _carried("model_output"), "share", True),
    ("Alt ajanların tüketim payı", lambda r: r["tokens"]["subagent_share"], "share", None),
    ("Ana sohbette Haiku/Sonnet çağrı payı", _cheap_share("main"), "share", False),
    ("Alt ajanlarda Haiku/Sonnet çağrı payı", _cheap_share("subagent"), "share", False),
]


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for label, get, fmt, lower_better in METRICS:
        try:
            b, a = get(before), get(after)
        except (KeyError, TypeError, ZeroDivisionError):
            b = a = None
        change = None
        if b is not None and a is not None:
            change = (a - b) if fmt == "share" else ((a - b) / b if b else None)
        better = None if change is None or change == 0 or lower_better is None else (change < 0) == lower_better
        rows.append({"metric": label, "format": fmt, "before": b, "after": a, "change": change, "better": better})
    small = after["scope"]["sessions"] < MIN_SESSIONS or after["tokens"]["calls"] < MIN_CALLS
    return {
        "before": {k: before["scope"].get(k) for k in ("sessions", "first", "last")} | {"calls": before["tokens"]["calls"]},
        "after": {k: after["scope"].get(k) for k in ("sessions", "first", "last")} | {"calls": after["tokens"]["calls"]},
        "small_sample": small,
        "rows": rows,
    }


def _num(value: float, digits: int = 0) -> str:
    """Turkish number format: 13.968 and 7,5."""
    return f"{value:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _fmt(value: float | None, fmt: str) -> str:
    if value is None:
        return "-"
    if fmt == "share":
        return f"%{_num(100 * value, 1)}"
    if fmt == "tokens":
        return f"{_num(value / 1000)}K" if value >= 1000 else _num(value)
    return _num(value, 1)


def _change(row: dict[str, Any]) -> str:
    change = row["change"]
    if change is None:
        return "-"
    if abs(change) < 0.0005:
        return "değişmedi"
    sign = "+" if change > 0 else "-"
    text = f"{sign}{_num(abs(100 * change), 1)} puan" if row["format"] == "share" else f"{sign}%{_num(abs(100 * change))}"
    return text + {True: "  iyi", False: "  kötü", None: ""}[row["better"]]


def render(result: dict[str, Any], events: dict[str, int] | None = None,
           labels: tuple[str, str] = ("Önce", "Sonra"), title_suffix: str = "") -> str:
    def period(side: dict[str, Any]) -> str:
        span = f"{(side['first'] or '')[:10]} - {(side['last'] or '')[:10]}"
        return f"{side['sessions']} oturum, {_num(side['calls'])} çağrı ({span})"

    lines = [
        f"Service001 önce / sonra karşılaştırması{title_suffix}",
        "",
        f"{labels[0] + ':':<16}{period(result['before'])}",
        f"{labels[1] + ':':<16}{period(result['after'])}",
        "",
    ]
    if result["small_sample"]:
        lines += [
            f"Uyarı: Sonraki dönemde {MIN_SESSIONS} oturum veya {_num(MIN_CALLS)} çağrıdan az veri var. Sonuçlar yanıltıcı olabilir.",
            "",
        ]
    width = max(len(r["metric"]) for r in result["rows"]) + 2
    lines.append(f"{'Ölçüt':<{width}}{'Önce':>10}{'Sonra':>10}   Değişim")  # columns: labels[0], labels[1]
    for row in result["rows"]:
        lines.append(
            f"{row['metric']:<{width}}{_fmt(row['before'], row['format']):>10}{_fmt(row['after'], row['format']):>10}   {_change(row)}"
        )
    if events:
        lines += ["", "Sonraki dönemde koruyucu olayları:"]
        lines += [f"- {kind}: {count}" for kind, count in sorted(events.items(), key=lambda kv: -kv[1])]
    lines += [
        "",
        "Notlar:",
        "- İki dönemdeki işler farklıdır. Bu yüzden toplamlar değil, çağrı başına değerler, medyanlar ve paylar karşılaştırılır.",
        "- Asıl ölçüt çağrı başına işlenen token. Diğer satırlar değişimin nereden geldiğini gösterir.",
        "- \"-\" o ölçütün eski raporda olmadığı veya o dönemde hiç verisi olmadığı anlamına gelir.",
    ]
    return "\n".join(lines)
