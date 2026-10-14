from __future__ import annotations

import pytest

from llm_gateway.report import build_report, render_text


def _row(i: int, **kw) -> dict:
    row = {
        "id": str(i),
        "ts": f"2026-10-14T10:00:{i:02d}.000+00:00",
        "tenant": "default",
        "app": "chatbot",
        "api": "openai",
        "model": "gpt-4o",
        "status_code": 200,
        "stream": 0,
        "input_tokens": 1000,
        "output_tokens": 100,
        "cached_input_tokens": 0,
        "usage_source": "provider",
        "est_system_tokens": 600,
        "est_tools_tokens": 200,
        "est_history_tokens": 0,
        "est_tool_results_tokens": 0,
        "est_user_prompt_tokens": 200,
        "est_input_tokens": 1000,
        "est_method": "tiktoken:o200k_base",
        "prompt_hash": f"p{i}",
        "prefix_hash": "prefix-a",
        "user_prompt_hash": f"u{i}",
        "lang": "tr",
        "text_chars": 3000,
        "text_tokens": 900,
        "latency_ms": 1000.0 + i,
        "cost_usd": 0.01,
        "cost_output_usd": 0.004,
        "max_tokens": None,
        "temperature": 0.0,
    }
    row.update(kw)
    return row


def test_report_numbers():
    rows = [
        _row(1),
        _row(2, prompt_hash="p1", user_prompt_hash="u1"),  # exact duplicate of 1
        _row(3, user_prompt_hash="u1"),  # same question, new context
        _row(4, cached_input_tokens=800, max_tokens=200),
        _row(5, est_history_tokens=3000, est_input_tokens=4000, prefix_hash="prefix-b", lang="other", text_chars=4000, text_tokens=900),
        _row(6, status_code=429, error="rate_limit_error"),
        _row(7, api="passthrough", path="/v1/models"),
    ]
    rep = build_report(rows, days=7)

    assert rep["overview"]["llm_requests"] == 6
    assert rep["overview"]["successful"] == 5
    assert rep["overview"]["failed"] == 1
    assert rep["overview"]["passthrough_requests"] == 1
    assert rep["tokens"]["input"] == 5000
    assert rep["cost"]["total_usd"] == pytest.approx(0.05)
    assert rep["cost"]["output_share"] == pytest.approx(0.4)

    op = rep["opportunities"]
    assert op["exact_duplicates"]["requests"] == 1
    assert op["exact_duplicates"]["deterministic_requests"] == 1
    assert op["repeated_questions_new_context"]["requests"] == 1
    # prefix-a has 4 requests: 3 reusable copies of an 800-token prefix, 800 already cached.
    assert op["stable_prefix"]["prefix_groups"] == 1
    assert op["stable_prefix"]["requests"] == 4
    assert op["stable_prefix"]["already_cached_tokens"] == 800
    assert op["context_heavy"]["requests"] == 1
    assert op["no_max_tokens"]["requests"] == 4

    langs = {lg["lang"]: lg for lg in rep["languages"]}
    assert langs["tr"]["requests"] == 4
    assert langs["tr"]["chars_per_token"] < langs["other"]["chars_per_token"]

    text = render_text(rep)
    assert "OPTİMİZASYON FIRSATLARI" in text
    assert "gpt-4o" in text


def test_report_tenant_filter_and_empty():
    rows = [_row(1, tenant="a"), _row(2, tenant="b")]
    assert build_report(rows, tenant="a")["overview"]["llm_requests"] == 1
    empty = build_report([], days=7)
    assert empty["overview"]["llm_requests"] == 0
    assert "kayıtlı LLM isteği yok" in render_text(empty)


def test_cache_opportunities_do_not_cross_tenants_or_models():
    rows = [
        _row(1, tenant="a"),
        _row(2, tenant="b", prompt_hash="p1"),  # same prompt, other tenant: not cacheable
        _row(3, tenant="a", model="other-model"),  # same prefix text, other model
    ]
    op = build_report(rows)["opportunities"]
    assert op["exact_duplicates"]["requests"] == 0
    assert op["stable_prefix"]["prefix_groups"] == 0
