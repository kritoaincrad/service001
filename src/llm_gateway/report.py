"""Measurement report: where do the tokens go, and what could be optimized?"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from cc_optimizer.i18n import L, language

from .analysis import SEGMENTS


def segment_labels() -> dict[str, str]:
    return {
        "system": L("System prompt", "Sistem promptu"),
        "tools": L("Tool definitions and schemas", "Tool tanımları ve şemalar"),
        "history": L("Chat history", "Sohbet geçmişi"),
        "tool_results": L("Tool results", "Tool sonuçları"),
        "user_prompt": L("Last user message", "Son kullanıcı mesajı"),
    }


def since_iso(days: float, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).isoformat(timespec="milliseconds")


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1)


def _sum(rows: Iterable[dict], key: str) -> int | float:
    return sum((r.get(key) or 0) for r in rows)


def _ratio(a: float, b: float) -> float:
    return a / b if b else 0.0


def _succeeded(r: dict) -> bool:
    return 200 <= (r.get("status_code") or 0) < 300


def build_report(rows: list[dict[str, Any]], *, days: float | None = None, tenant: str | None = None) -> dict[str, Any]:
    if tenant is not None:
        rows = [r for r in rows if r.get("tenant") == tenant]
    rows = sorted(rows, key=lambda r: r.get("ts") or "")
    llm = [r for r in rows if r.get("api") in ("openai", "anthropic")]
    ok = [r for r in llm if _succeeded(r)]
    failed = [r for r in llm if not _succeeded(r)]

    input_tokens = _sum(ok, "input_tokens")
    output_tokens = _sum(ok, "output_tokens")
    cached = _sum(ok, "cached_input_tokens")

    # Calibration: provider-reported input vs our tokenizer estimate.
    calib_rows = [r for r in ok if r.get("usage_source") == "provider" and (r.get("est_input_tokens") or 0) > 0]
    calibration = _ratio(_sum(calib_rows, "input_tokens"), _sum(calib_rows, "est_input_tokens")) or 1.0

    seg_totals = {s: _sum(ok, f"est_{s}_tokens") for s in SEGMENTS}
    seg_all = sum(seg_totals.values())

    priced = [r for r in ok if r.get("cost_usd") is not None]
    total_cost = _sum(priced, "cost_usd")
    output_cost = _sum(priced, "cost_output_usd")

    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_model[r.get("model") or r.get("response_model") or "?"].append(r)
    by_owner: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in ok:
        by_owner[(r.get("tenant") or "-", r.get("app") or "-")].append(r)

    # Opportunity: identical requests (exact cache).
    duplicates: list[dict] = []
    repeated_questions = 0
    seen_prompts: set[tuple] = set()
    seen_questions: set[tuple] = set()
    for r in ok:
        # A cache never crosses tenants, so duplicates only count within one tenant.
        h = (r.get("tenant"), r["prompt_hash"]) if r.get("prompt_hash") else None
        q = (r.get("tenant"), r["user_prompt_hash"]) if r.get("user_prompt_hash") else None
        if h and h in seen_prompts:
            duplicates.append(r)
        elif q and q in seen_questions:
            repeated_questions += 1
        if h:
            seen_prompts.add(h)
        if q:
            seen_questions.add(q)

    # Opportunity: repeated system prompt + tools (provider prompt caching).
    # Provider prompt caching is scoped to one account and one model.
    prefix_groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in ok:
        if r.get("prefix_hash"):
            prefix_groups[(r.get("tenant"), r.get("provider"), r.get("model"), r["prefix_hash"])].append(r)
    reused = {h: g for h, g in prefix_groups.items() if len(g) >= 2}
    reusable_est = sum(
        (r.get("est_system_tokens") or 0) + (r.get("est_tools_tokens") or 0) for g in reused.values() for r in g[1:]
    )
    reusable = round(reusable_est * calibration)
    cached_in_groups = sum(_sum(g, "cached_input_tokens") for g in reused.values())

    # Opportunity: context-heavy requests (history compression).
    def context_share(r: dict) -> float:
        ctx = (r.get("est_history_tokens") or 0) + (r.get("est_tool_results_tokens") or 0)
        return _ratio(ctx, r.get("est_input_tokens") or 0)

    heavy = [r for r in ok if context_share(r) > 0.5]
    heavy_tokens = round(
        sum((r.get("est_history_tokens") or 0) + (r.get("est_tool_results_tokens") or 0) for r in heavy) * calibration
    )

    langs: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        langs[r.get("lang") or "unknown"].append(r)

    streams_ok = [r for r in ok if r.get("stream") and r.get("ttft_ms") is not None]

    return {
        "period": {
            "days": days,
            "first_request": rows[0]["ts"] if rows else None,
            "last_request": rows[-1]["ts"] if rows else None,
            "tenant": tenant,
        },
        "overview": {
            "llm_requests": len(llm),
            "successful": len(ok),
            "failed": len(failed),
            "failure_reasons": [
                {"status_code": sc, "error": err, "count": n}
                for (sc, err), n in Counter((r.get("status_code"), r.get("error")) for r in failed).most_common(10)
            ],
            "streaming_share": _ratio(sum(1 for r in llm if r.get("stream")), len(llm)),
            "passthrough_requests": sum(1 for r in rows if r.get("api") == "passthrough"),
            "estimated_usage_requests": sum(1 for r in ok if r.get("usage_source") == "estimated"),
            "missing_usage_requests": sum(1 for r in ok if r.get("input_tokens") is None),
        },
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "cached_input": cached,
            "cache_write": _sum(ok, "cache_write_tokens"),
            "reasoning": _sum(ok, "reasoning_tokens"),
            "cached_share_of_input": _ratio(cached, input_tokens),
            "avg_input_per_request": round(_ratio(input_tokens, len(ok))),
            "avg_output_per_request": round(_ratio(output_tokens, len(ok))),
            "estimate_calibration": round(calibration, 3),
            "estimate_methods": sorted({r["est_method"] for r in ok if r.get("est_method")}),
        },
        "cost": {
            "total_usd": round(total_cost, 4),
            "output_usd": round(output_cost, 4),
            "output_share": _ratio(output_cost, total_cost),
            "priced_requests": len(priced),
            "unpriced_requests": len(ok) - len(priced),
            "unpriced_models": sorted({r.get("model") or "?" for r in ok if r.get("cost_usd") is None}),
        },
        "input_segments": {
            s: {"est_tokens": seg_totals[s], "share": _ratio(seg_totals[s], seg_all)} for s in SEGMENTS
        },
        "by_model": [
            {
                "model": m,
                "requests": len(g),
                "input": _sum(g, "input_tokens"),
                "output": _sum(g, "output_tokens"),
                "avg_input": round(_ratio(_sum(g, "input_tokens"), len(g))),
                "avg_output": round(_ratio(_sum(g, "output_tokens"), len(g))),
                "cost_usd": round(_sum(g, "cost_usd"), 4),
            }
            for m, g in sorted(by_model.items(), key=lambda kv: -_sum(kv[1], "input_tokens"))
        ],
        "by_tenant_app": [
            {
                "tenant": t,
                "app": a,
                "requests": len(g),
                "tokens": _sum(g, "input_tokens") + _sum(g, "output_tokens"),
                "cost_usd": round(_sum(g, "cost_usd"), 4),
            }
            for (t, a), g in sorted(by_owner.items(), key=lambda kv: -len(kv[1]))
        ],
        "latency_ms": {
            "p50": _pct([r["latency_ms"] for r in ok if r.get("latency_ms") is not None], 0.5),
            "p95": _pct([r["latency_ms"] for r in ok if r.get("latency_ms") is not None], 0.95),
            "ttft_p50": _pct([r["ttft_ms"] for r in streams_ok], 0.5),
            "ttft_p95": _pct([r["ttft_ms"] for r in streams_ok], 0.95),
        },
        "opportunities": {
            "exact_duplicates": {
                "requests": len(duplicates),
                "share": _ratio(len(duplicates), len(ok)),
                "deterministic_requests": sum(1 for r in duplicates if r.get("temperature") == 0),
                "tokens": _sum(duplicates, "input_tokens") + _sum(duplicates, "output_tokens"),
                "cost_usd": round(_sum(duplicates, "cost_usd"), 4),
            },
            "repeated_questions_new_context": {
                "requests": repeated_questions,
                "share": _ratio(repeated_questions, len(ok)),
            },
            "stable_prefix": {
                "prefix_groups": len(reused),
                "requests": sum(len(g) for g in reused.values()),
                "reusable_input_tokens": reusable,
                "already_cached_tokens": cached_in_groups,
                "cache_gap_tokens": max(reusable - cached_in_groups, 0),
            },
            "context_heavy": {
                "requests": len(heavy),
                "share": _ratio(len(heavy), len(ok)),
                "context_tokens": heavy_tokens,
            },
            "no_max_tokens": {
                "requests": sum(1 for r in ok if r.get("max_tokens") is None),
                "share": _ratio(sum(1 for r in ok if r.get("max_tokens") is None), len(ok)),
            },
        },
        "languages": [
            {
                "lang": lang,
                "requests": len(g),
                "share": _ratio(len(g), len(ok)),
                "chars_per_token": round(_ratio(_sum(g, "text_chars"), _sum(g, "text_tokens")), 2),
            }
            for lang, g in sorted(langs.items(), key=lambda kv: -len(kv[1]))
        ],
    }


# ---------------------------------------------------------------- rendering


def _n(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.2f}"
    return f"{int(v):,}"


def _p(v: float) -> str:
    return f"%{v * 100:.1f}" if language() == "tr" else f"{v * 100:.1f}%"


def _table(headers: list[str], rows: list[list[str]], left: int = 1) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = ["  " + line, "  " + "  ".join("-" * w for w in widths)]
    for r in rows:
        out.append("  " + "  ".join(c.ljust(w) if i < left else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths))))
    return out


def render_text(rep: dict[str, Any]) -> str:
    ov, tk, cost, op = rep["overview"], rep["tokens"], rep["cost"], rep["opportunities"]
    lines: list[str] = []
    period = rep["period"]
    lines.append(L("LLM GATEWAY MEASUREMENT REPORT", "LLM GATEWAY ÖLÇÜM RAPORU"))
    lines.append(L(f"Period: {period['first_request'] or '-'}  ->  {period['last_request'] or '-'}", f"Dönem: {period['first_request'] or '-'}  ->  {period['last_request'] or '-'}"))
    if period.get("tenant"):
        lines.append(L(f"Tenant: {period['tenant']}", f"Kiracı: {period['tenant']}"))
    lines.append("")

    if not ov["llm_requests"]:
        lines.append(L("No LLM requests recorded in this period.", "Bu dönemde kayıtlı LLM isteği yok."))
        return "\n".join(lines)

    lines.append(L("OVERVIEW", "GENEL"))
    lines.append(L(f"  LLM requests: {_n(ov['llm_requests'])}  (successful {_n(ov['successful'])}, failed {_n(ov['failed'])})", f"  LLM isteği: {_n(ov['llm_requests'])}  (başarılı {_n(ov['successful'])}, hatalı {_n(ov['failed'])})"))
    lines.append(L(f"  Streaming share: {_p(ov['streaming_share'])}", f"  Streaming oranı: {_p(ov['streaming_share'])}"))
    lines.append(L(f"  Other API calls (passthrough): {_n(ov['passthrough_requests'])}", f"  Diğer API çağrıları (passthrough): {_n(ov['passthrough_requests'])}"))
    if ov["estimated_usage_requests"]:
        lines.append(L(f"  Estimated because the provider reported no usage: {_n(ov['estimated_usage_requests'])}", f"  Sağlayıcı kullanım bilgisi vermediği için tahmin edilen: {_n(ov['estimated_usage_requests'])}"))
    for fr in ov["failure_reasons"]:
        lines.append(L(f"  Error {fr['status_code']} {fr['error'] or ''}: {_n(fr['count'])}", f"  Hata {fr['status_code']} {fr['error'] or ''}: {_n(fr['count'])}"))
    lines.append("")

    lines.append("TOKEN")
    lines.append(L(f"  Input: {_n(tk['input'])}   Output: {_n(tk['output'])}   Reasoning: {_n(tk['reasoning'])}", f"  Girdi: {_n(tk['input'])}   Çıktı: {_n(tk['output'])}   Reasoning: {_n(tk['reasoning'])}"))
    lines.append(L(f"  Input read from cache: {_n(tk['cached_input'])} ({_p(tk['cached_share_of_input'])})   Cache writes: {_n(tk['cache_write'])}", f"  Cache'ten okunan girdi: {_n(tk['cached_input'])} ({_p(tk['cached_share_of_input'])})   Cache yazımı: {_n(tk['cache_write'])}"))
    lines.append(L(f"  Average per request: input {_n(tk['avg_input_per_request'])}, output {_n(tk['avg_output_per_request'])}", f"  İstek başına ortalama: girdi {_n(tk['avg_input_per_request'])}, çıktı {_n(tk['avg_output_per_request'])}"))
    lines.append("")

    lines.append(L("COST", "MALİYET"))
    if cost["priced_requests"]:
        lines.append(L(f"  Total: ${cost['total_usd']:,.4f}   Output share: {_p(cost['output_share'])}", f"  Toplam: ${cost['total_usd']:,.4f}   Çıktı payı: {_p(cost['output_share'])}"))
    if cost["unpriced_requests"]:
        lines.append(
            L(f"  {_n(cost['unpriced_requests'])} requests have no price defined. "
            f"Add them to the pricing section in config.yaml: {', '.join(cost['unpriced_models'])}", f"  Fiyatı tanımlı olmayan {_n(cost['unpriced_requests'])} istek var. "
            f"config.yaml içindeki pricing bölümüne ekleyin: {', '.join(cost['unpriced_models'])}")
        )
    lines.append("")

    lines.append(L("WHERE INPUT TOKENS GO (estimated breakdown)", "GİRDİ TOKEN'LARI NEREYE GİDİYOR (tahmini dağılım)"))
    labels = segment_labels()
    seg_rows = [
        [labels[s], _n(v["est_tokens"]), _p(v["share"])] for s, v in rep["input_segments"].items()
    ]
    lines += _table([L("Segment", "Bölüm"), "Token", L("Share", "Pay")], seg_rows)
    lines.append(L(f"  Estimation method: {', '.join(tk['estimate_methods']) or '-'}  Calibration factor: {tk['estimate_calibration']}", f"  Tahmin yöntemi: {', '.join(tk['estimate_methods']) or '-'}  Kalibrasyon katsayısı: {tk['estimate_calibration']}"))
    lines.append("")

    lines.append(L("OPTIMIZATION OPPORTUNITIES", "OPTİMİZASYON FIRSATLARI"))
    d = op["exact_duplicates"]
    lines.append(
        L(f"  Exact duplicate requests: {_n(d['requests'])} ({_p(d['share'])}), "
        f"{_n(d['tokens'])} tokens, ${d['cost_usd']:,.4f}. {_n(d['deterministic_requests'])} of them use temperature=0.", f"  Birebir tekrar eden istek: {_n(d['requests'])} ({_p(d['share'])}), "
        f"{_n(d['tokens'])} token, ${d['cost_usd']:,.4f}. Bunların {_n(d['deterministic_requests'])} tanesi temperature=0.")
    )
    q = op["repeated_questions_new_context"]
    lines.append(L(f"  Same question, different context: {_n(q['requests'])} ({_p(q['share'])}). Semantic cache candidate.", f"  Aynı soru, farklı bağlam: {_n(q['requests'])} ({_p(q['share'])}). Semantik cache adayı."))
    sp = op["stable_prefix"]
    lines.append(
        L(f"  Repeated system prompt and tool definitions: {_n(sp['prefix_groups'])} groups, {_n(sp['requests'])} requests. "
        f"Cacheable ~{_n(sp['reusable_input_tokens'])} tokens, currently read from cache {_n(sp['already_cached_tokens'])}. "
        f"Missed: ~{_n(sp['cache_gap_tokens'])} tokens.", f"  Tekrar eden sistem promptu ve tool tanımı: {_n(sp['prefix_groups'])} grup, {_n(sp['requests'])} istek. "
        f"Cache'lenebilir ~{_n(sp['reusable_input_tokens'])} token, şu an cache'ten okunan {_n(sp['already_cached_tokens'])}. "
        f"Kaçırılan: ~{_n(sp['cache_gap_tokens'])} token.")
    )
    ch = op["context_heavy"]
    lines.append(
        L(f"  Requests where more than half of the input is history and tool results: {_n(ch['requests'])} ({_p(ch['share'])}), "
        f"~{_n(ch['context_tokens'])} tokens. Context compression candidate.", f"  Girdisinin yarısından fazlası geçmiş ve tool sonucu olan istek: {_n(ch['requests'])} ({_p(ch['share'])}), "
        f"~{_n(ch['context_tokens'])} token. Bağlam sıkıştırma adayı.")
    )
    nm = op["no_max_tokens"]
    lines.append(L(f"  Requests without an output limit (max_tokens): {_n(nm['requests'])} ({_p(nm['share'])})", f"  Çıktı limiti (max_tokens) olmayan istek: {_n(nm['requests'])} ({_p(nm['share'])})"))
    lines.append("")

    lines.append(L("MODELS", "MODELLER"))
    lines += _table(
        [
            "Model",
            L("Requests", "İstek"),
            L("Input", "Girdi"),
            L("Output", "Çıktı"),
            L("Avg. input", "Ort. girdi"),
            L("Avg. output", "Ort. çıktı"),
            L("Cost $", "Maliyet $"),
        ],
        [
            [m["model"], _n(m["requests"]), _n(m["input"]), _n(m["output"]), _n(m["avg_input"]), _n(m["avg_output"]), f"{m['cost_usd']:,.4f}"]
            for m in rep["by_model"]
        ],
    )
    lines.append("")

    lines.append(L("TENANT AND APP", "KİRACI VE UYGULAMA"))
    lines += _table(
        [L("Tenant", "Kiracı"), L("App", "Uygulama"), L("Requests", "İstek"), "Token", L("Cost $", "Maliyet $")],
        [[o["tenant"], o["app"], _n(o["requests"]), _n(o["tokens"]), f"{o['cost_usd']:,.4f}"] for o in rep["by_tenant_app"]],
        left=2,
    )
    lines.append("")

    lines.append(L("LANGUAGE", "DİL"))
    lines += _table(
        [L("Language", "Dil"), L("Requests", "İstek"), L("Share", "Pay"), L("Chars/token", "Karakter/token")],
        [[lg["lang"], _n(lg["requests"]), _p(lg["share"]), _n(lg["chars_per_token"])] for lg in rep["languages"]],
    )
    lines.append("")

    lat = rep["latency_ms"]
    lines.append(L("LATENCY (ms)", "GECİKME (ms)"))
    ms = {k: (None if v is None else round(v)) for k, v in lat.items()}
    lines.append(L(f"  Total p50 {_n(ms['p50'])}, p95 {_n(ms['p95'])}   First token p50 {_n(ms['ttft_p50'])}, p95 {_n(ms['ttft_p95'])}", f"  Toplam p50 {_n(ms['p50'])}, p95 {_n(ms['p95'])}   İlk token p50 {_n(ms['ttft_p50'])}, p95 {_n(ms['ttft_p95'])}"))
    return "\n".join(lines)
