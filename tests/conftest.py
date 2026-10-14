from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from llm_gateway.app import create_app
from llm_gateway.config import Settings
from llm_gateway.store import RequestStore


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c


def sse_response(payload: bytes, chunk_size: int | None = None) -> httpx.Response:
    if chunk_size:
        chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
    else:
        chunks = [payload]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(chunks))


def openai_sse(events: list[dict], newline: bytes = b"\n") -> bytes:
    sep = newline + newline
    body = b"".join(b"data: " + json.dumps(e).encode() + sep for e in events)
    return body + b"data: [DONE]" + sep


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "providers": [
            {"name": "openai", "type": "openai", "base_url": "https://upstream.test/v1", "models": ["gpt-"]},
            {"name": "anthropic", "type": "anthropic", "base_url": "https://anthropic.test/v1", "models": ["claude-"]},
            {
                "name": "local",
                "type": "openai",
                "base_url": "http://local.test/v1",
                "models": ["llama"],
                "supports_stream_usage": False,
            },
        ],
        "default_provider": "openai",
        "pricing": {"gpt-4o": {"input": 2.0, "output": 8.0, "cached_input": 1.0}},
    }
    base.update(overrides)
    return Settings.model_validate(base)


class Harness:
    """The gateway app wired to a fake upstream and an in-memory store."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response], settings: Settings | None = None) -> None:
        self.calls: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return handler(request)

        self.store = RequestStore(":memory:")
        self.app = create_app(settings or make_settings(), transport=httpx.MockTransport(recording), store=self.store)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            resp = await client.request(method, url, **kwargs)
        await self.app.state.gateway.drain()
        return resp

    def rows(self) -> list[dict]:
        return self.store.fetch()


@pytest.fixture(autouse=True)
def no_background_history_sync(monkeypatch):
    """Session start hooks would otherwise start real syncs over the user's transcripts."""
    monkeypatch.setattr("cc_optimizer.history.maybe_sync_in_background", lambda *a, **k: False)
