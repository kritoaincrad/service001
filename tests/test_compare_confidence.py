from __future__ import annotations

from cc_optimizer import compare as cmp
from cc_optimizer import i18n
from tests.test_compare import report


def rows_by_key(result):
    return {r["key"]: r for r in result["rows"]}


def steady(value, n=6, wobble=0.02):
    """n sessions whose own value stays close to `value`."""
    return [value * (1 + wobble * ((i % 3) - 1)) for i in range(n)]


def test_a_large_steady_change_is_clear():
    before, after = report(20, 10_000, 4_000_000_000, 360_000, 0.175), report(8, 2_000, 600_000_000, 180_000, 0.05)
    samples = {"context_per_call_p50": (steady(360_000), steady(180_000))}
    row = rows_by_key(cmp.compare(before, after, samples))["context_per_call_p50"]
    assert row["confidence"] == "clear" and row["better"] is True
    low, high = row["interval"]
    assert low < -0.4 and high < -0.4  # the whole interval is a reduction of about half


def test_a_change_inside_the_noise_is_unclear_and_not_called_better():
    before, after = report(20, 10_000, 4_000_000_000, 360_000, 0.175), report(8, 2_000, 600_000_000, 350_000, 0.05)
    noisy = [200_000, 520_000, 330_000, 610_000, 150_000, 480_000]
    samples = {"context_per_call_p50": (noisy, [300_000, 560_000, 180_000, 640_000, 260_000, 500_000])}
    result = cmp.compare(before, after, samples)
    row = rows_by_key(result)["context_per_call_p50"]
    assert row["confidence"] == "unclear" and row["interval"][0] < 0 < row["interval"][1]
    text = cmp.render(result)
    line = next(ln for ln in text.splitlines() if ln.startswith("Context per call") or ln.startswith("Çağrı başına bağlam"))
    assert "belirsiz" in line and "iyi" not in line


def test_too_few_sessions_is_insufficient():
    before, after = report(20, 10_000, 4_000_000_000, 360_000, 0.175), report(8, 2_000, 600_000_000, 180_000, 0.05)
    samples = {"context_per_call_p50": (steady(360_000), steady(180_000, n=2))}
    row = rows_by_key(cmp.compare(before, after, samples))["context_per_call_p50"]
    assert row["confidence"] == "insufficient" and row["interval"] is None


def test_without_session_values_the_old_behaviour_is_kept():
    before, after = report(20, 10_000, 4_000_000_000, 360_000, 0.175), report(8, 2_000, 600_000_000, 180_000, 0.05)
    result = cmp.compare(before, after)
    assert all(r["confidence"] is None and r["interval"] is None for r in result["rows"])
    assert "iyi" in cmp.render(result) and "belirsiz" not in cmp.render(result)


def test_ratio_samples_use_the_headline_definition():
    # sessions of very different size: the side value is sum/sum, not an average of ratios
    assert cmp._statistic([(300.0, 1.0), (100.0, 9.0)]) == 40.0
    assert cmp._statistic([1.0, 5.0, 2.0]) == 2.0  # plain values: median over sessions


def test_the_bootstrap_is_reproducible():
    a, b = steady(100.0), steady(80.0)
    assert cmp._bootstrap(a, b, "tokens") == cmp._bootstrap(a, b, "tokens")


def test_different_projects_trigger_a_warning_unless_matched():
    before, after = report(20, 10_000, 4_000_000_000, 360_000, 0.175), report(8, 2_000, 600_000_000, 180_000, 0.05)
    result = cmp.compare(before, after)
    result["projects"] = {"before": {"Shop": 0.9, "Docs": 0.1}, "after": {"Tool": 1.0}, "overlap": 0.0, "matched": False}
    assert "farklı projeleri" in cmp.render(result) and "--matched" in cmp.render(result)
    result["projects"]["matched"] = True
    assert "farklı projeleri" not in cmp.render(result)
    result["projects"] = {"before": {"Shop": 1.0}, "after": {"Shop": 1.0}, "overlap": 1.0, "matched": False}
    assert "farklı projeleri" not in cmp.render(result)


def test_project_overlap_and_mix():
    from cc_optimizer.history import project_overlap

    assert project_overlap({"A": 0.5, "B": 0.5}, {"A": 1.0}) == 0.5
    assert project_overlap({"A": 1.0}, {"B": 1.0}) == 0.0
    assert project_overlap({"A": 0.3, "B": 0.7}, {"A": 0.3, "B": 0.7}) == 1.0


def test_markdown_report_and_guard_effect(monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_LANG", "en")
    i18n.reset()
    before, after = report(20, 10_000, 4_000_000_000, 360_000, 0.175), report(8, 2_000, 600_000_000, 180_000, 0.05)
    result = cmp.compare(before, after, {"context_per_call_p50": (steady(360_000), steady(180_000))})
    effect = {"blocked": 5, "blocked_tokens": 98_000, "retried_tokens": 9_000}
    text = cmp.render_markdown(result, {"Subagent call": 9}, title_suffix=" (Shop)", guard_tokens=effect)
    assert text.startswith("## Service001 before / after comparison (Shop)")
    assert "| Metric | Before | After | Change | 90% interval |" in text
    assert "| Context per call, median | 360K | 180K | -50%  better |" in text
    assert "about 98K tokens (5 blocked reads)" in text and "9K of it was dumped anyway" in text
    assert cmp.render(result, guard_tokens={"blocked": 0, "blocked_tokens": 0, "retried_tokens": 0}).count("Kept out") == 0


def test_period_labels_are_aligned_whatever_their_length(monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_LANG", "en")
    i18n.reset()
    before, after = report(20, 10_000, 4_000_000_000, 360_000, 0.175), report(8, 2_000, 600_000_000, 180_000, 0.05)
    text = cmp.render(cmp.compare(before, after), labels=("Without Service001", "With Service001"))
    first, second = (ln for ln in text.splitlines() if ln.startswith(("Without Service001:", "With Service001:")))
    assert first.startswith("Without Service001: ") and second.startswith("With Service001:    ")
