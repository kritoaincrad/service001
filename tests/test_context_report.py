from __future__ import annotations

import json

from cc_optimizer import context_report
from cc_optimizer.transcripts import TokenCounter


def _transcript(tmp_path, records):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def _att(kind, content):
    return {"type": "attachment", "attachment": {"type": kind, "content": content}}


def _call(creation, read):
    return {"type": "assistant", "message": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": creation,
                                                       "cache_read_input_tokens": read}}}


def test_measure_splits_sources_and_keeps_the_remainder(tmp_path):
    path = _transcript(tmp_path, [
        _att("skill_listing", "- a: x\n- b: y" * 200),
        _att("agent_listing_delta", "- scout: finds\n- Explore: searches\n- worker: builds"),
        _att("hook_additional_context", "rules " * 50),
        _att("hook_success", "echo " * 500),       # echo of the hook output, must not be counted
        _att("prompt_snapshot", "state " * 500),   # client state, must not be counted
        _call(10_000, 30_000),
        _att("skill_listing", "after the first call"),
    ])
    result = context_report.measure(path, TokenCounter(fast=True))

    assert result["first_call"] == 40_002
    kinds = {r["kind"]: r for r in result["rows"]}
    assert set(kinds) == {"skill_listing", "agent_listing_delta", "our_agents", "hook_additional_context"}
    assert kinds["our_agents"]["owner"] == "service001" and kinds["hook_additional_context"]["owner"] == "service001"
    assert result["counted"] + result["remainder"] == result["first_call"]
    text = context_report.render(result)
    assert "Skill list" in text and "Service001" in text


def test_no_model_call_gives_none(tmp_path):
    assert context_report.measure(_transcript(tmp_path, [_att("skill_listing", "x")])) is None
    assert "No session" in context_report.render(None) or "bulunamadı" in context_report.render(None)


def test_latest_transcript_matches_part_of_the_project_name(tmp_path):
    for name in ("c--work-alpha", "c--work-beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "one.jsonl").write_text("{}", encoding="utf-8")
    assert context_report.latest_transcript(tmp_path, "Beta").parent.name == "c--work-beta"
    assert context_report.latest_transcript(tmp_path, "gamma") is None
