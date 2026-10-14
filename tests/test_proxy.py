from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from conftest import Harness, make_settings, openai_sse, sse_response

SYSTEM_TR = "Sen bir bankanın müşteri hizmetleri asistanısın. Kısa ve net cevap ver. " * 10


def _completion(content: str = "Merhaba!") -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "gpt-4o-2024-08-06",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 1000}},
    }


def test_openai_non_stream_is_transparent_and_logged():
    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "https://upstream.test/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer client-key"
        assert "x-gateway-app" not in req.headers
        return httpx.Response(200, json=_completion())

    h = Harness(handler)
    body = {
        "model": "gpt-4o",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_TR},
            {"role": "user", "content": "Merhaba, kredi kartı borcumu nasıl öğrenebilirim?"},
        ],
    }
    resp = asyncio.run(
        h.request("POST", "/v1/chat/completions", json=body, headers={"authorization": "Bearer client-key", "x-gateway-app": "chatbot"})
    )

    assert resp.status_code == 200
    assert resp.json() == _completion()
    assert resp.headers["x-gateway-request-id"]
    assert json.loads(h.calls[0].content) == body  # forwarded unchanged

    [row] = h.rows()
    assert row["api"] == "openai" and row["provider"] == "openai"
    assert row["tenant"] == "default" and row["app"] == "chatbot"
    assert (row["input_tokens"], row["output_tokens"], row["cached_input_tokens"]) == (1200, 30, 1000)
    assert row["usage_source"] == "provider"
    assert row["response_model"] == "gpt-4o-2024-08-06"
    assert row["finish_reason"] == "stop"
    assert row["est_system_tokens"] > row["est_user_prompt_tokens"] > 0
    assert row["est_history_tokens"] == 0
    assert row["lang"] == "tr"
    assert row["temperature"] == 0
    assert row["max_tokens"] is None
    # 200 uncached * $2 + 1000 cached * $1 + 30 output * $8, per million tokens
    assert row["cost_usd"] == pytest.approx(1640 / 1e6)
    assert row["cost_output_usd"] == pytest.approx(240 / 1e6)
    assert "Merhaba" not in json.dumps(row, ensure_ascii=False)  # no prompt content stored


STREAM_EVENTS = [
    {"id": "c", "model": "gpt-4o", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
    {"id": "c", "model": "gpt-4o", "choices": [{"index": 0, "delta": {"content": "Mer"}, "finish_reason": None}]},
    {"id": "c", "model": "gpt-4o", "choices": [{"index": 0, "delta": {"content": "haba"}, "finish_reason": "stop"}]},
]
USAGE_EVENT = {"id": "c", "model": "gpt-4o", "choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 2}}


def _stream_text(resp: httpx.Response) -> str:
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            for c in json.loads(line[6:])["choices"]:
                out.append(c["delta"].get("content") or "")
    return "".join(out)


def test_openai_stream_injects_usage_and_hides_it_from_client():
    payload = openai_sse(STREAM_EVENTS + [USAGE_EVENT], newline=b"\r\n")

    def handler(req: httpx.Request) -> httpx.Response:
        sent = json.loads(req.content)
        assert sent["stream_options"] == {"include_usage": True}
        return sse_response(payload, chunk_size=7)  # splits events and \r\n pairs

    h = Harness(handler)
    body = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "Selam"}]}
    resp = asyncio.run(h.request("POST", "/v1/chat/completions", json=body))

    assert resp.status_code == 200
    assert _stream_text(resp) == "Merhaba"
    assert '"usage"' not in resp.text
    assert resp.text.endswith("data: [DONE]\n\n")

    [row] = h.rows()
    assert (row["input_tokens"], row["output_tokens"]) == (50, 2)
    assert row["stream"] == 1 and row["completed"] == 1
    assert row["ttft_ms"] is not None
    assert row["finish_reason"] == "stop"


def test_openai_stream_keeps_usage_when_client_asked_for_it():
    payload = openai_sse(STREAM_EVENTS + [USAGE_EVENT])
    h = Harness(lambda req: sse_response(payload))
    body = {
        "model": "gpt-4o",
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "Selam"}],
    }
    resp = asyncio.run(h.request("POST", "/v1/chat/completions", json=body))

    assert resp.content == payload
    assert json.loads(h.calls[0].content) == body
    assert h.rows()[0]["input_tokens"] == 50


def test_stream_without_provider_usage_is_estimated():
    payload = openai_sse([{**e, "model": "llama3.1"} for e in STREAM_EVENTS])

    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url).startswith("http://local.test/v1/")
        assert "stream_options" not in json.loads(req.content)
        return sse_response(payload)

    h = Harness(handler)
    body = {"model": "llama3.1", "stream": True, "messages": [{"role": "user", "content": "Bir fıkra anlat"}]}
    resp = asyncio.run(h.request("POST", "/v1/chat/completions", json=body))

    assert resp.status_code == 200
    [row] = h.rows()
    assert row["provider"] == "local"
    assert row["usage_source"] == "estimated"
    assert row["input_tokens"] > 0 and row["output_tokens"] > 0
    assert row["cost_usd"] is None


def test_anthropic_stream_usage_and_byte_exact_relay():
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "m", "model": "claude-x", "usage": {
            "input_tokens": 10, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Selam"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 15}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    payload = b"".join(f"event: {n}\ndata: {json.dumps(d)}\n\n".encode() for n, d in events)

    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "https://anthropic.test/v1/messages"
        assert req.headers["x-api-key"] == "sk-ant-client"
        assert req.headers["anthropic-version"] == "2023-06-01"
        return sse_response(payload, chunk_size=11)

    h = Harness(handler)
    body = {
        "model": "claude-x",
        "max_tokens": 512,
        "stream": True,
        "system": [{"type": "text", "text": "You are a helpful assistant."}],
        "messages": [{"role": "user", "content": "Hello there, how are you today?"}],
    }
    resp = asyncio.run(h.request("POST", "/v1/messages", json=body, headers={"x-api-key": "sk-ant-client"}))

    assert resp.content == payload
    [row] = h.rows()
    assert row["api"] == "anthropic"
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (2010, 2000, 15)
    assert row["finish_reason"] == "end_turn"
    assert row["max_tokens"] == 512
    assert row["lang"] == "other"


def test_upstream_error_is_forwarded_and_logged():
    err = {"error": {"type": "rate_limit_error", "message": "slow down"}}
    h = Harness(lambda req: httpx.Response(429, json=err))
    resp = asyncio.run(h.request("POST", "/v1/chat/completions", json={"model": "gpt-4o", "messages": []}))

    assert resp.status_code == 429
    assert resp.json() == err
    [row] = h.rows()
    assert row["status_code"] == 429 and row["error"] == "rate_limit_error"


def test_upstream_error_on_stream_request_is_forwarded():
    err = {"error": {"type": "invalid_request_error", "message": "bad"}}
    h = Harness(lambda req: httpx.Response(400, json=err))
    resp = asyncio.run(h.request("POST", "/v1/chat/completions", json={"model": "gpt-4o", "stream": True, "messages": []}))

    assert resp.status_code == 400
    assert resp.json() == err
    assert h.rows()[0]["error"] == "invalid_request_error"


def test_connection_failure_returns_502():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    h = Harness(handler)
    resp = asyncio.run(h.request("POST", "/v1/chat/completions", json={"model": "gpt-4o", "messages": []}))

    assert resp.status_code == 502
    [row] = h.rows()
    assert row["status_code"] == 502 and row["error"] == "ConnectError"


def test_gateway_keys_and_upstream_key_injection(monkeypatch):
    monkeypatch.setenv("TEST_UPSTREAM_KEY", "secret")
    settings = make_settings(gateway_keys={"gw-key-1": "team-a"})
    settings.providers[0].api_key_env = "TEST_UPSTREAM_KEY"

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer secret"
        assert "x-gateway-key" not in req.headers
        return httpx.Response(200, json=_completion())

    h = Harness(handler, settings)
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}

    denied = asyncio.run(h.request("POST", "/v1/chat/completions", json=body))
    assert denied.status_code == 401
    wrong = asyncio.run(h.request("POST", "/v1/chat/completions", json=body, headers={"x-gateway-key": "nope"}))
    assert wrong.status_code == 401

    ok = asyncio.run(
        h.request("POST", "/v1/chat/completions", json=body, headers={"x-gateway-key": "gw-key-1", "authorization": "Bearer must-not-leak"})
    )
    assert ok.status_code == 200
    [row] = h.rows()
    assert row["tenant"] == "team-a"


def test_missing_upstream_key_env_is_a_500(monkeypatch):
    monkeypatch.delenv("TEST_MISSING_KEY", raising=False)
    settings = make_settings()
    settings.providers[0].api_key_env = "TEST_MISSING_KEY"
    h = Harness(lambda req: httpx.Response(200, json=_completion()), settings)
    resp = asyncio.run(h.request("POST", "/v1/chat/completions", json={"model": "gpt-4o", "messages": []}))
    assert resp.status_code == 500
    assert "TEST_MISSING_KEY" in resp.json()["error"]["message"]
    assert h.calls == []


def test_provider_override_header_and_unknown_provider():
    h = Harness(lambda req: httpx.Response(200, json=_completion()))
    body = {"model": "gpt-4o", "messages": []}

    asyncio.run(h.request("POST", "/v1/chat/completions", json=body, headers={"x-gateway-provider": "local"}))
    assert str(h.calls[-1].url) == "http://local.test/v1/chat/completions"

    bad = asyncio.run(h.request("POST", "/v1/chat/completions", json=body, headers={"x-gateway-provider": "nope"}))
    assert bad.status_code == 400

    wrong_api = asyncio.run(h.request("POST", "/v1/chat/completions", json=body, headers={"x-gateway-provider": "anthropic"}))
    assert wrong_api.status_code == 400


def test_query_string_is_forwarded():
    h = Harness(lambda req: httpx.Response(200, json=_completion()))
    asyncio.run(h.request("POST", "/v1/chat/completions?api-version=2024-10-21", json={"model": "gpt-4o", "messages": []}))
    assert str(h.calls[0].url) == "https://upstream.test/v1/chat/completions?api-version=2024-10-21"


def test_invalid_json_body_is_rejected():
    h = Harness(lambda req: httpx.Response(200))
    resp = asyncio.run(h.request("POST", "/v1/chat/completions", content=b"not json", headers={"content-type": "application/json"}))
    assert resp.status_code == 400
    assert h.calls == []


def test_passthrough_other_endpoints():
    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "https://upstream.test/v1/models?limit=5"
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    h = Harness(handler)
    resp = asyncio.run(h.request("GET", "/v1/models?limit=5", headers={"authorization": "Bearer k"}))
    assert resp.json() == {"data": [{"id": "gpt-4o"}]}
    [row] = h.rows()
    assert row["api"] == "passthrough" and row["path"] == "/v1/models" and row["status_code"] == 200


def test_stats_endpoint():
    h = Harness(lambda req: httpx.Response(200, json=_completion()))
    asyncio.run(h.request("POST", "/v1/chat/completions", json={"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]}))
    stats = asyncio.run(h.request("GET", "/gateway/stats?days=1")).json()
    assert stats["overview"]["llm_requests"] == 1
    assert stats["tokens"]["input"] == 1200
