from __future__ import annotations

from cc_optimizer.compare import compare, render


def report(sessions, calls, processed, ctx, read_share, main_models=None, large_reads=10):
    tokens = {"calls": calls, "processed": processed, "subagent_share": 0.05}
    if main_models is not None:
        tokens["calls_by_model"] = {"main": main_models, "subagent": {}}
    return {
        "scope": {"sessions": sessions, "first": "2026-10-01T00:00:00Z", "last": "2026-10-10T00:00:00Z"},
        "tokens": tokens,
        "context": {"per_call_p50": ctx, "pre_compact_p50": None, "startup_p50": 40_000},
        "reads": {"large_full_reads": {"count": large_reads}},
        "sources": [{"name": "tool:Read", "carried_share": read_share},
                    {"name": "tool:Bash", "carried_share": 0.10}, {"name": "tool:PowerShell", "carried_share": 0.02}],
    }


def rows(result):
    return {r["metric"]: r for r in result["rows"]}


def test_compare_normalizes_and_judges_direction():
    before = report(20, 10_000, 4_000_000_000, 360_000, 0.175)
    after = report(8, 2_000, 600_000_000, 180_000, 0.05, main_models={"claude-opus-5-5": 60, "claude-sonnet-5-5": 40})
    result = compare(before, after)
    r = rows(result)
    per_call = r["Çağrı başına işlenen token"]
    assert per_call["before"] == 400_000 and per_call["after"] == 300_000 and per_call["better"] is True
    assert round(r["Yükte dosya okumalarının payı"]["change"], 3) == -0.125
    assert round(r["Yükte kabuk çıktılarının payı"]["before"], 3) == 0.12  # Bash + PowerShell
    assert r["Sınırsız büyük okuma, 1000 çağrıda"]["after"] == 5.0
    assert r["Alt ajanların tüketim payı"]["better"] is None  # neither direction is better by itself
    assert r["Ana sohbette Haiku/Sonnet çağrı payı"]["before"] is None  # field missing in old report
    assert r["Ana sohbette Haiku/Sonnet çağrı payı"]["after"] == 0.4
    assert r["Sıkıştırma öncesi bağlam, medyan"]["change"] is None
    assert result["small_sample"] is False


def test_render_turkish_formatting_and_small_sample_warning():
    before = report(20, 13_968, 5_555_000_000, 362_230, 0.175)
    after = report(1, 174, 30_000_000, 150_000, 0.001)
    text = render(compare(before, after), {"Bağlam uzunluğu uyarısı": 1})
    assert "13.968 çağrı" in text and "Uyarı:" in text
    assert "362K" in text and "-%59  iyi" in text and "-17,4 puan  iyi" in text
    assert "- Bağlam uzunluğu uyarısı: 1" in text
