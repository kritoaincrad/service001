"""Turkish text rendering of the analysis."""

from __future__ import annotations

from typing import Any


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
    return f"%{share * 100:.1f}"


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _cut_path(text: str, width: int) -> str:
    """Keep the end of a path, where the file name is."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


_PATH_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}


def _table(headers: list[str], rows: list[list[str]], left: int = 1) -> list[str]:
    if not rows:
        return ["  (kayıt yok)"]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    out = ["  " + "  ".join(h.ljust(w) if i < left else h.rjust(w) for i, (h, w) in enumerate(zip(headers, widths)))]
    out.append("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  " + "  ".join(c.ljust(w) if i < left else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths))))
    return out


def source_label(name: str) -> str:
    if name.startswith("tool:"):
        return f"Araç çıktısı: {name[5:]}"
    if name.startswith("attachment:"):
        return f"Otomatik ek: {name[11:]}"
    return {
        "prompt": "Kullanıcı mesajları",
        "summary": "Sıkıştırma özetleri",
        "fixed": "Sabit yük: sistem promptu, araçlar, CLAUDE.md",
        "model_output": "Model çıktıları: yazılan kod ve metin",
    }.get(name, name)


def render(rep: dict[str, Any]) -> str:
    sc, tk, cx, est = rep["scope"], rep["tokens"], rep["context"], rep["estimation"]
    L: list[str] = []
    L.append("CLAUDE CODE TOKEN ANALİZİ")
    L.append(f"Kapsam: {sc['projects']} proje, {sc['sessions']} oturum, {sc['subagent_timelines']} alt ajan")
    L.append(f"Dönem: {sc['first'] or '-'}  ->  {sc['last'] or '-'}")
    L.append("")
    if not tk["calls"]:
        L.append("Seçilen kapsamda model çağrısı bulunamadı.")
        return "\n".join(L)

    processed = tk["processed"] or 1
    L.append("1. TOKEN TÜKETİMİ")
    L.append(f"  Model çağrısı: {tk['calls']:,}   İşlenen toplam token: {_k(tk['processed'])}")
    L += _table(
        ["Tür", "Token", "Pay"],
        [
            ["Cache'ten okunan bağlam", _k(tk["cache_read"]), _p(tk["cache_read"] / processed)],
            ["Cache'e yazılan bağlam", _k(tk["cache_creation"]), _p(tk["cache_creation"] / processed)],
            ["Cache'siz girdi", _k(tk["input"]), _p(tk["input"] / processed)],
            ["Çıktı (düşünme dahil)", _k(tk["output"]), _p(tk["output"] / processed)],
        ],
    )
    L.append(f"  Çıktının tahmini düşünme kısmı: {_k(tk['thinking_est'])} ({_p(tk['thinking_est'] / (tk['output'] or 1))})")
    L.append(f"  Alt ajanların payı: {_p(tk['subagent_share'])}")
    L.append("")
    L += _table(
        ["Model", "Çağrı", "Cache okuma", "Cache yazma", "Girdi", "Çıktı"],
        [[m["model"], f"{m['calls']:,}", _k(m["cache_read"]), _k(m["cache_creation"]), _k(m["input"]), _k(m["output"])]
         for m in tk["by_model"]],
    )
    L.append("")

    L.append("2. BAĞLAM BÜYÜKLÜĞÜ")
    L.append(f"  Çağrı başına bağlam: medyan {_k(cx['per_call_p50'])}, p95 {_k(cx['per_call_p95'])}, en yüksek {_k(cx['max'])}")
    L.append(
        f"  Oturum açılışındaki sabit yük (sistem promptu, araç tanımları, CLAUDE.md, ilk mesaj): "
        f"medyan {_k(cx['startup_p50'])}, en yüksek {_k(cx['startup_max'])}"
    )
    comp = cx["compactions"]
    if comp:
        parts = ", ".join(f"{k}: {v}" for k, v in comp.items())
        L.append(f"  Bağlam sıkıştırma: {sum(comp.values())} kez ({parts}). Sıkıştırma öncesi medyan bağlam {_k(cx['pre_compact_p50'])}")
    else:
        L.append("  Bağlam sıkıştırma: yok")
    L.append("")
    L.append(f"  Senaryo: otomatik sıkıştırma daha erken tetiklenseydi (sıkıştırma sonrası bağlam ~{_k(cx['post_compact_p50'])})")
    L += _table(
        ["Sıkıştırma eşiği", "Bağlam token'ı", "Tasarruf", "Ek sıkıştırma"],
        [[_k(w["threshold"]), _k(w["simulated_tokens"]), _p(w["saving_share"]), f"{w['extra_compactions']:,}"]
         for w in cx["what_if_compaction"]],
    )
    L.append("  Ek sıkıştırmanın maliyeti hesaba katıldı. Sıkıştırma ayrıntı kaybettirir, eşik düştükçe bu risk artar.")
    L.append("")

    L.append("3. BAĞLAMI NE DOLDURUYOR")
    L.append("  Boyut: bağlama girdiğinde. Taşınan: sonraki her çağrıda yeniden okunmasıyla oluşan toplam yük.")
    L += _table(
        ["Kaynak", "Adet", "Boyut", "Taşınan", "Pay"],
        [[source_label(s["name"]), f"{s['count']:,}", _k(s["tokens"]), _k(s["carried_tokens"]), _p(s["carried_share"])]
         for s in rep["sources"][:15]],
    )
    L.append("")

    L.append("  Model çıktılarının kırılımı:")
    L += _table(
        ["Tür", "Boyut", "Taşınan"],
        [[o["name"], _k(o["tokens"]), _k(o["carried_tokens"])] for o in rep["model_output_parts"]],
    )
    L.append("")

    L.append("4. EN PAHALI ARAÇ ÇIKTILARI")
    L += _table(
        ["Araç", "Hedef", "Boyut", "Taşındığı çağrı", "Taşınan"],
        [[t["tool"] + (" (alt ajan)" if t["subagent"] else ""),
          (_cut_path if t["tool"] in _PATH_TOOLS else _cut)(t["target"], 60), _k(t["tokens"]),
          f"{t['carried_calls']:,}", _k(t["carried_tokens"])] for t in rep["top_outputs"]],
        left=2,
    )
    L.append("")

    L.append("5. KABUK KOMUTLARI")
    L += _table(
        ["Komut", "Adet", "Boyut", "Taşınan"],
        [[g["name"], f"{g['count']:,}", _k(g["tokens"]), _k(g["carried_tokens"])] for g in rep["shell_groups"]],
    )
    L.append("")

    rd = rep["reads"]
    L.append("6. DOSYA OKUMALARI")
    type_labels = {"text": "metin", "image": "görsel", "file_unchanged": "değişmemiş (Claude Code atladı)",
                   "pdf": "pdf", "?": "türü kayıtlı değil"}
    types = ", ".join(f"{type_labels.get(k, k)}: {v:,}" for k, v in sorted(rd["by_type"].items(), key=lambda kv: -kv[1]))
    L.append(f"  Sonuç türleri: {types or '-'}")
    r = rd["redundant"]
    L.append(
        f"  Gereksiz tekrar okuma: {r['count']:,} kez. Dosya arada değişmemiş ve önceki okuma hâlâ bağlamdaydı. "
        f"Boyut {_k(r['tokens'])}, taşınan {_k(r['carried_tokens'])}"
    )
    L.append(f"  Sınır verilmeden okunan büyük dosya: {rd['large_full_reads']['count']:,} kez")
    if rd["most_read_files"]:
        L.append("  En çok okunan dosyalar:")
        L += _table(
            ["Dosya", "Okuma", "Boyut", "Taşınan"],
            [[_cut_path(f["name"], 70), f"{f['count']:,}", _k(f["tokens"]), _k(f["carried_tokens"])] for f in rd["most_read_files"]],
        )
    L.append("")

    L.append("7. EN ÇOK TÜKETEN OTURUMLAR")
    L += _table(
        ["Oturum", "Çağrı", "Token", "En büyük bağlam", "Sıkıştırma", "Alt ajan"],
        [[_cut(s["title"] or s["session_id"][:8], 50), f"{s['calls']:,}", _k(s["tokens"]), _k(s["max_context"]),
          str(s["compactions"]), str(s["subagents"])] for s in rep["sessions"]],
    )
    L.append("")

    L.append("NOTLAR")
    L.append(
        f"  Kaynak boyutları {est['method']} ile tahmin edildi ve gerçek bağlam büyümesine göre "
        f"x{est['calibration']} ile ölçeklendi ({est['calibration_samples']} örnek)."
    )
    L.append(
        f"  Bu kaynaklar, kayıttaki gerçek bağlam tüketiminin {_p(est['explained_share'])} kadarını açıklıyor. "
        "Model çıktılarının bağlamda ne kadar kaldığı yaklaşıktır."
    )
    L.append("  Görseller sabit 1.500 token sayıldı. Model çağrısı token'ları kayıttaki gerçek değerlerdir.")
    L.append("  Kayıt formatı Claude Code'un iç formatıdır ve sürümle değişebilir.")
    return "\n".join(L)
