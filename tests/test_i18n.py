from __future__ import annotations

import json

from cc_optimizer import compare, doctor, history, i18n
from cc_optimizer.i18n import L


def test_language_resolution_order(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"language": "tr"}), encoding="utf-8")
    monkeypatch.setenv("CC_OPTIMIZER_LANG", "en")
    i18n.reset()
    assert i18n.language() == "en"  # the environment variable wins over the settings file
    monkeypatch.delenv("CC_OPTIMIZER_LANG")
    i18n.reset()
    assert i18n.language() == "tr"  # then the settings file
    (tmp_path / "config.json").write_text(json.dumps({"language": "de"}), encoding="utf-8")
    monkeypatch.setattr(i18n, "_system_language", lambda: "en")
    i18n.reset()
    assert i18n.language() == "en"  # unsupported values fall back to the system language


def test_system_language_turkish_is_detected(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path))
    monkeypatch.delenv("CC_OPTIMIZER_LANG")
    monkeypatch.setattr(i18n, "_system_language", lambda: "tr")
    i18n.reset()
    assert L("Hello", "Merhaba") == "Merhaba"


def test_numbers_follow_the_language(monkeypatch):
    monkeypatch.setenv("CC_OPTIMIZER_LANG", "en")
    i18n.reset()
    assert history._num(400000) == "400,000" and i18n.decimal("1,234.5") == "1,234.5"
    monkeypatch.setenv("CC_OPTIMIZER_LANG", "tr")
    i18n.reset()
    assert history._num(400000) == "400.000" and i18n.decimal("1,234.5") == "1.234,5"


def test_english_output_has_no_turkish(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CC_OPTIMIZER_LANG", "en")
    monkeypatch.setenv("CC_OPTIMIZER_HOME", str(tmp_path))
    i18n.reset()
    text = doctor.render(doctor.check_project(tmp_path))
    assert not any(ch in text for ch in "çğıöşüÇĞİÖŞÜ")
    assert compare.metrics()[0][0].isascii()


def test_english_plurals():
    assert i18n.plural(1, "session") == "1 session"
    assert i18n.plural(0, "session") == "0 sessions"
    assert i18n.plural(2, "warning") == "2 warnings"


def test_compare_rows_have_language_independent_keys(monkeypatch):
    from tests.test_compare import report

    assert len(compare.METRIC_KEYS) == len(compare.metrics()) == len(set(compare.METRIC_KEYS))
    results = {}
    for lang in ("en", "tr"):
        monkeypatch.setenv("CC_OPTIMIZER_LANG", lang)
        i18n.reset()
        results[lang] = compare.compare(report(20, 10_000, 4_000_000_000, 360_000, 0.175),
                                        report(8, 2_000, 600_000_000, 180_000, 0.05))
    assert [r["key"] for r in results["en"]["rows"]] == [r["key"] for r in results["tr"]["rows"]]
    assert results["en"]["rows"][0]["metric"] != results["tr"]["rows"][0]["metric"]


def test_output_part_names_are_data_and_labels_follow_the_language(monkeypatch):
    from cc_optimizer.report import part_label

    monkeypatch.setenv("CC_OPTIMIZER_LANG", "en")
    i18n.reset()
    assert part_label("text") == "Text" and part_label("Bash") == "Bash call"
    assert part_label("Bash çağrısı") == "Bash çağrısı"  # a report saved by an older version
    monkeypatch.setenv("CC_OPTIMIZER_LANG", "tr")
    i18n.reset()
    assert part_label("text") == "Metin" and part_label("Bash") == "Bash çağrısı"
