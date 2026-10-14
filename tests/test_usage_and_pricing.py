from __future__ import annotations

import json
import sqlite3

import pytest

from llm_gateway.config import ModelPrice
from llm_gateway.pricing import compute_cost, find_price
from llm_gateway.store import RequestStore
from llm_gateway.usage import StreamTracker, Usage, parse_usage


def test_parse_usage_anthropic_includes_cache_in_total():
    u = parse_usage({"input_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20, "output_tokens": 7}, "anthropic")
    assert (u.input_total, u.cached_input, u.cache_write, u.output) == (125, 100, 20, 7)


def test_parse_usage_openai_chat_and_responses():
    chat = parse_usage({"prompt_tokens": 10, "completion_tokens": 3, "completion_tokens_details": {"reasoning_tokens": 2}}, "openai")
    assert (chat.input_total, chat.output, chat.reasoning) == (10, 3, 2)
    responses = parse_usage({"input_tokens": 40, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 32}}, "openai")
    assert (responses.input_total, responses.cached_input) == (40, 32)
    assert parse_usage(None, "openai") is None
    assert parse_usage({}, "anthropic") is None


def _event(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode()


def test_stream_tracker_only_strips_when_asked():
    usage_chunk = _event({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    assert StreamTracker("openai", strip_usage_only_chunks=True).feed(usage_chunk) is False
    assert StreamTracker("openai", strip_usage_only_chunks=False).feed(usage_chunk) is True
    # A content chunk with a null usage field is never stripped.
    content_chunk = _event({"choices": [{"delta": {"content": "x"}}], "usage": None})
    assert StreamTracker("openai", strip_usage_only_chunks=True).feed(content_chunk) is True


def test_stream_tracker_responses_api():
    t = StreamTracker("openai")
    t.feed(_event({"type": "response.output_text.delta", "delta": "Hi"}))
    t.feed(_event({"type": "response.completed", "response": {"model": "gpt-x", "status": "completed",
                                                              "usage": {"input_tokens": 9, "output_tokens": 1}}}))
    assert t.output_text == "Hi"
    assert (t.usage.input_total, t.response_model, t.finish_reason) == (9, "gpt-x", "completed")


def test_find_price_prefers_exact_then_longest_prefix():
    pricing = {
        "gpt-4o": ModelPrice(input=2, output=8),
        "gpt-4o-mini": ModelPrice(input=0.1, output=0.4),
    }
    assert find_price(pricing, "gpt-4o-mini-2024-07-18").input == 0.1
    assert find_price(pricing, "gpt-4o-2024-08-06").input == 2
    assert find_price(pricing, "unknown", "gpt-4o").input == 2
    assert find_price(pricing, "claude-x") is None


def test_compute_cost_with_cache_rates():
    price = ModelPrice(input=3, output=15, cached_input=0.3, cache_write=3.75)
    total, out = compute_cost(price, Usage(input_total=1100, output=100, cached_input=1000, cache_write=50))
    assert total == pytest.approx((50 * 3 + 1000 * 0.3 + 50 * 3.75 + 100 * 15) / 1e6)
    assert out == pytest.approx(1500 / 1e6)
    assert compute_cost(None, Usage(input_total=1, output=1)) == (None, None)


def test_store_adds_missing_columns(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE requests (id TEXT PRIMARY KEY, ts TEXT NOT NULL)")
    conn.commit()
    conn.close()

    store = RequestStore(str(db))
    store.insert({"id": "a", "ts": "2026-01-01T00:00:00.000+00:00", "model": "m", "unknown_field": 1})
    [row] = store.fetch()
    assert row["model"] == "m"
    store.close()
