"""Turkish text rendering of the analysis."""

from __future__ import annotations

from typing import Any

from .i18n import L, language, plural


def part_label(kind: str) -> str:
    """Display name of a model output part: "text" or a tool name. Names already translated by an older
    report file (saved with `analyze --json`) are shown as they are."""
    if kind == "text":
        return L("Text", "Metin")
    if kind in ("Metin",) or kind.endswith((" call", " çağrısı")):
        return kind
    return L(f"{kind} call", f"{kind} çağrısı")


def _k(n: float | int | None) -> str:
    """Compact number: 1.234.567 -> 1.2M."""
    if n is None:
        return "-"
    n = float(n)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= size:
            return f"{n / size:.1f}{suffix}"
    return f"{n:.0f}"


def _p(share: float) -> str:
    return f"%{share * 100:.1f}" if language() == "tr" else f"{share * 100:.1f}%"


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _cut_path(text: str, width: int) -> str:
    """Keep the end of a path, where the file name is."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


_PATH_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}


def _table(headers: list[str], rows: list[list[str]], left: int = 1) -> list[str]:
    if not rows:
        return [L("  (no records)", "  (kayıt yok)")]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    out = ["  " + "  ".join(h.ljust(w) if i < left else h.rjust(w) for i, (h, w) in enumerate(zip(headers, widths)))]
    out.append("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  " + "  ".join(c.ljust(w) if i < left else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths))))
    return out


def source_label(name: str) -> str:
    if name.startswith("tool:"):
        return L(f"Tool output: {name[5:]}", f"Araç çıktısı: {name[5:]}")
    if name.startswith("attachment:"):
        return L(f"Automatic attachment: {name[11:]}", f"Otomatik ek: {name[11:]}")
    return {
        "prompt": L("User messages", "Kullanıcı mesajları"),
        "summary": L("Compaction summaries", "Sıkıştırma özetleri"),
        "fixed": L("Fixed load: system prompt, tools, CLAUDE.md", "Sabit yük: sistem promptu, araçlar, CLAUDE.md"),
        "model_output": L("Model output: written code and text", "Model çıktıları: yazılan kod ve metin"),
    }.get(name, name)


def render(rep: dict[str, Any]) -> str:
    sc, tk, cx, est = rep["scope"], rep["tokens"], rep["context"], rep["estimation"]
    out: list[str] = []
    out.append(L("CLAUDE CODE TOKEN ANALYSIS", "CLAUDE CODE TOKEN ANALİZİ"))
    out.append(L(f"Scope: {plural(sc['projects'], 'project')}, {plural(sc['sessions'], 'session')}, {plural(sc['subagent_timelines'], 'subagent')}",
                 f"Kapsam: {sc['projects']} proje, {sc['sessions']} oturum, {sc['subagent_timelines']} alt ajan"))
    out.append(L(f"Period: {sc['first'] or '-'}  ->  {sc['last'] or '-'}", f"Dönem: {sc['first'] or '-'}  ->  {sc['last'] or '-'}"))
    out.append("")
    if not tk["calls"]:
        out.append(L("No model calls found in the selected scope.", "Seçilen kapsamda model çağrısı bulunamadı."))
        return "\n".join(out)

    processed = tk["processed"] or 1
    out.append(L("1. TOKEN USAGE", "1. TOKEN TÜKETİMİ"))
    out.append(L(f"  Model calls: {tk['calls']:,}   Total tokens processed: {_k(tk['processed'])}",
                 f"  Model çağrısı: {tk['calls']:,}   İşlenen toplam token: {_k(tk['processed'])}"))
    out += _table(
        [L("Type", "Tür"), "Token", L("Share", "Pay")],
        [
            [L("Context read from cache", "Cache'ten okunan bağlam"), _k(tk["cache_read"]), _p(tk["cache_read"] / processed)],
            [L("Context written to cache", "Cache'e yazılan bağlam"), _k(tk["cache_creation"]), _p(tk["cache_creation"] / processed)],
            [L("Uncached input", "Cache'siz girdi"), _k(tk["input"]), _p(tk["input"] / processed)],
            [L("Output (including thinking)", "Çıktı (düşünme dahil)"), _k(tk["output"]), _p(tk["output"] / processed)],
        ],
    )
    out.append(L(f"  Estimated thinking part of the output: {_k(tk['thinking_est'])} ({_p(tk['thinking_est'] / (tk['output'] or 1))})",
                 f"  Çıktının tahmini düşünme kısmı: {_k(tk['thinking_est'])} ({_p(tk['thinking_est'] / (tk['output'] or 1))})"))
    out.append(L(f"  Subagent share: {_p(tk['subagent_share'])}", f"  Alt ajanların payı: {_p(tk['subagent_share'])}"))
    out.append("")
    out += _table(
        ["Model", L("Calls", "Çağrı"), L("Cache read", "Cache okuma"), L("Cache write", "Cache yazma"), L("Input", "Girdi"), L("Output", "Çıktı")],
        [[m["model"], f"{m['calls']:,}", _k(m["cache_read"]), _k(m["cache_creation"]), _k(m["input"]), _k(m["output"])]
         for m in tk["by_model"]],
    )
    out.append("")

    out.append(L("2. CONTEXT SIZE", "2. BAĞLAM BÜYÜKLÜĞÜ"))
    out.append(L(f"  Context per call: median {_k(cx['per_call_p50'])}, p95 {_k(cx['per_call_p95'])}, max {_k(cx['max'])}",
                 f"  Çağrı başına bağlam: medyan {_k(cx['per_call_p50'])}, p95 {_k(cx['per_call_p95'])}, en yüksek {_k(cx['max'])}"))
    out.append(L(
        f"  Fixed load at session start (system prompt, tool definitions, CLAUDE.md, first message): "
        f"median {_k(cx['startup_p50'])}, max {_k(cx['startup_max'])}",
        f"  Oturum açılışındaki sabit yük (sistem promptu, araç tanımları, CLAUDE.md, ilk mesaj): "
        f"medyan {_k(cx['startup_p50'])}, en yüksek {_k(cx['startup_max'])}",
    ))
    comp = cx["compactions"]
    if comp:
        parts = ", ".join(f"{k}: {v}" for k, v in comp.items())
        out.append(L(
            f"  Context compaction: {sum(comp.values())} times ({parts}). Median context before compaction {_k(cx['pre_compact_p50'])}",
            f"  Bağlam sıkıştırma: {sum(comp.values())} kez ({parts}). Sıkıştırma öncesi medyan bağlam {_k(cx['pre_compact_p50'])}",
        ))
    else:
        out.append(L("  Context compaction: none", "  Bağlam sıkıştırma: yok"))
    out.append("")
    out.append(L(
        f"  Scenario: if automatic compaction had triggered earlier (context after compaction ~{_k(cx['post_compact_p50'])})",
        f"  Senaryo: otomatik sıkıştırma daha erken tetiklenseydi (sıkıştırma sonrası bağlam ~{_k(cx['post_compact_p50'])})",
    ))
    out += _table(
        [L("Compaction threshold", "Sıkıştırma eşiği"), L("Context tokens", "Bağlam token'ı"), L("Saving", "Tasarruf"), L("Extra compactions", "Ek sıkıştırma")],
        [[_k(w["threshold"]), _k(w["simulated_tokens"]), _p(w["saving_share"]), f"{w['extra_compactions']:,}"]
         for w in cx["what_if_compaction"]],
    )
    out.append(L("  The cost of extra compactions is included. Compaction loses detail, and this risk grows as the threshold drops.",
                 "  Ek sıkıştırmanın maliyeti hesaba katıldı. Sıkıştırma ayrıntı kaybettirir, eşik düştükçe bu risk artar."))
    out.append("")

    out.append(L("3. WHAT FILLS THE CONTEXT", "3. BAĞLAMI NE DOLDURUYOR"))
    out.append(L("  Size: when it entered the context. Carried: total load from being re-read on every later call.",
                 "  Boyut: bağlama girdiğinde. Taşınan: sonraki her çağrıda yeniden okunmasıyla oluşan toplam yük."))
    out += _table(
        [L("Source", "Kaynak"), L("Count", "Adet"), L("Size", "Boyut"), L("Carried", "Taşınan"), L("Share", "Pay")],
        [[source_label(s["name"]), f"{s['count']:,}", _k(s["tokens"]), _k(s["carried_tokens"]), _p(s["carried_share"])]
         for s in rep["sources"][:15]],
    )
    out.append("")

    out.append(L("  Model output breakdown:", "  Model çıktılarının kırılımı:"))
    out += _table(
        [L("Type", "Tür"), L("Size", "Boyut"), L("Carried", "Taşınan")],
        [[part_label(o["name"]), _k(o["tokens"]), _k(o["carried_tokens"])] for o in rep["model_output_parts"]],
    )
    out.append("")

    out.append(L("4. MOST EXPENSIVE TOOL OUTPUTS", "4. EN PAHALI ARAÇ ÇIKTILARI"))
    out += _table(
        [L("Tool", "Araç"), L("Target", "Hedef"), L("Size", "Boyut"), L("Calls carried", "Taşındığı çağrı"), L("Carried", "Taşınan")],
        [[t["tool"] + (L(" (subagent)", " (alt ajan)") if t["subagent"] else ""),
          (_cut_path if t["tool"] in _PATH_TOOLS else _cut)(t["target"], 60), _k(t["tokens"]),
          f"{t['carried_calls']:,}", _k(t["carried_tokens"])] for t in rep["top_outputs"]],
        left=2,
    )
    out.append("")

    out.append(L("5. SHELL COMMANDS", "5. KABUK KOMUTLARI"))
    out += _table(
        [L("Command", "Komut"), L("Count", "Adet"), L("Size", "Boyut"), L("Carried", "Taşınan")],
        [[g["name"], f"{g['count']:,}", _k(g["tokens"]), _k(g["carried_tokens"])] for g in rep["shell_groups"]],
    )
    out.append("")

    rd = rep["reads"]
    out.append(L("6. FILE READS", "6. DOSYA OKUMALARI"))
    type_labels = {"text": L("text", "metin"), "image": L("image", "görsel"),
                   "file_unchanged": L("unchanged (skipped by Claude Code)", "değişmemiş (Claude Code atladı)"),
                   "pdf": "pdf", "?": L("type not recorded", "türü kayıtlı değil")}
    types = ", ".join(f"{type_labels.get(k, k)}: {v:,}" for k, v in sorted(rd["by_type"].items(), key=lambda kv: -kv[1]))
    out.append(L(f"  Result types: {types or '-'}", f"  Sonuç türleri: {types or '-'}"))
    r = rd["redundant"]
    out.append(L(
        f"  Redundant re-reads: {r['count']:,} times. The file had not changed and the earlier read was still in context. "
        f"Size {_k(r['tokens'])}, carried {_k(r['carried_tokens'])}",
        f"  Gereksiz tekrar okuma: {r['count']:,} kez. Dosya arada değişmemiş ve önceki okuma hâlâ bağlamdaydı. "
        f"Boyut {_k(r['tokens'])}, taşınan {_k(r['carried_tokens'])}",
    ))
    out.append(L(f"  Large files read without a limit: {rd['large_full_reads']['count']:,} times",
                 f"  Sınır verilmeden okunan büyük dosya: {rd['large_full_reads']['count']:,} kez"))
    if rd["most_read_files"]:
        out.append(L("  Most-read files:", "  En çok okunan dosyalar:"))
        out += _table(
            [L("File", "Dosya"), L("Reads", "Okuma"), L("Size", "Boyut"), L("Carried", "Taşınan")],
            [[_cut_path(f["name"], 70), f"{f['count']:,}", _k(f["tokens"]), _k(f["carried_tokens"])] for f in rd["most_read_files"]],
        )
    out.append("")

    out.append(L("7. SESSIONS USING THE MOST", "7. EN ÇOK TÜKETEN OTURUMLAR"))
    out += _table(
        [L("Session", "Oturum"), L("Calls", "Çağrı"), "Token", L("Largest context", "En büyük bağlam"), L("Compactions", "Sıkıştırma"), L("Subagents", "Alt ajan")],
        [[_cut(s["title"] or s["session_id"][:8], 50), f"{s['calls']:,}", _k(s["tokens"]), _k(s["max_context"]),
          str(s["compactions"]), str(s["subagents"])] for s in rep["sessions"]],
    )
    out.append("")

    out.append(L("NOTES", "NOTLAR"))
    out.append(L(
        f"  Source sizes were estimated with {est['method']} and scaled by x{est['calibration']} "
        f"to match the real context growth ({est['calibration_samples']} samples).",
        f"  Kaynak boyutları {est['method']} ile tahmin edildi ve gerçek bağlam büyümesine göre "
        f"x{est['calibration']} ile ölçeklendi ({est['calibration_samples']} örnek).",
    ))
    out.append(L(
        f"  These sources explain {_p(est['explained_share'])} of the real context consumption in the log. "
        "How long model outputs stay in context is approximate.",
        f"  Bu kaynaklar, kayıttaki gerçek bağlam tüketiminin {_p(est['explained_share'])} kadarını açıklıyor. "
        "Model çıktılarının bağlamda ne kadar kaldığı yaklaşıktır.",
    ))
    out.append(L("  Images are counted as a flat 1,500 tokens. Model call tokens are the real values from the log.",
                 "  Görseller sabit 1.500 token sayıldı. Model çağrısı token'ları kayıttaki gerçek değerlerdir."))
    out.append(L("  The log format is internal to Claude Code and may change between versions.",
                 "  Kayıt formatı Claude Code'un iç formatıdır ve sürümle değişebilir."))
    return "\n".join(out)
