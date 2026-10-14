from __future__ import annotations

import pytest

from llm_gateway.analysis import TokenCounter, analyze_request, detect_language


@pytest.fixture(scope="module")
def counter() -> TokenCounter:
    return TokenCounter()


def test_openai_segments(counter):
    body = {
        "model": "gpt-4o",
        "max_completion_tokens": 300,
        "tools": [{"type": "function", "function": {"name": "get_balance", "parameters": {"type": "object"}}}],
        "messages": [
            {"role": "system", "content": "You are a banking assistant."},
            {"role": "user", "content": "What is my balance?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "get_balance", "arguments": "{\"account\": \"123\"}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "{\"balance\": 1520.75, \"currency\": \"TRY\"}"},
            {"role": "user", "content": [
                {"type": "text", "text": "And what about this receipt?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
        ],
    }
    a = analyze_request(body, "openai", counter)
    assert all(a.segments[s] > 0 for s in ("system", "tools", "history", "tool_results", "user_prompt"))
    assert a.message_count == 5
    assert a.attachment_count == 1
    assert a.max_tokens == 300
    assert a.est_input_tokens == sum(a.segments.values())


def test_anthropic_segments(counter):
    body = {
        "model": "claude-x",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "Sen bir sigorta asistanısın."}],
        "tools": [{"name": "lookup_policy", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "Poliçemi kontrol eder misin?"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "lookup_policy", "input": {"no": "42"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "Poliçe aktif, bitiş tarihi 2027-01-01."},
                {"type": "text", "text": "Teşekkürler, peki yenileme ücreti ne kadar?"},
            ]},
        ],
    }
    a = analyze_request(body, "anthropic", counter)
    assert all(a.segments[s] > 0 for s in ("system", "tools", "history", "tool_results", "user_prompt"))
    assert a.lang == "tr"


def test_hashes(counter):
    base = {"model": "gpt-4o", "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "Hello  World"}]}
    same_but_streamed = {**base, "stream": True, "user": "u-1"}
    other_question = {"model": "gpt-4o", "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "Other"}]}
    normalized = {"model": "gpt-4o", "messages": [{"role": "system", "content": "X"}, {"role": "user", "content": "hello world"}]}

    a, b, c, d = (analyze_request(x, "openai", counter) for x in (base, same_but_streamed, other_question, normalized))
    assert a.prompt_hash == b.prompt_hash
    assert a.prompt_hash != c.prompt_hash
    assert a.prefix_hash == c.prefix_hash
    assert a.prefix_hash != d.prefix_hash
    assert a.user_prompt_hash == d.user_prompt_hash


def test_no_prefix_hash_without_system_or_tools(counter):
    a = analyze_request({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "openai", counter)
    assert a.prefix_hash is None


def test_detect_language():
    assert detect_language("Merhaba, siparişimin durumunu öğrenmek istiyorum. Yardımcı olur musunuz?") == "tr"
    assert detect_language("Bu bir deneme metni ve kısa bir cümle olarak yazıldı") == "tr"
    assert detect_language("Hello, I would like to know the status of my order. Can you help me?") == "other"
    assert detect_language("ok") == "unknown"


def test_turkish_costs_more_tokens_per_char_than_english(counter):
    # Sanity check for the language statistics in the report.
    tr = "Müşterilerimizin başvurularını değerlendirirken kullandığımız kriterleri açıklayabilir misiniz?"
    en = "Could you explain the criteria we use when evaluating the applications of our customers?"
    assert len(tr) / counter.count(tr) < len(en) / counter.count(en)
