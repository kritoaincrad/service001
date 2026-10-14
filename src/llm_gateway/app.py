"""FastAPI application: the transparent measurement proxy."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .analysis import RequestAnalysis, TokenCounter, analyze_request
from .config import ApiType, ProviderConfig, Settings
from .pricing import compute_cost, find_price
from .report import build_report, since_iso
from .store import RequestStore
from .upstream import (
    GatewayConfigError,
    RoutingError,
    build_upstream_headers,
    filter_response_headers,
    resolve_provider,
    upstream_url,
)
from .usage import ResponseInfo, StreamTracker, Usage, info_from_json

log = logging.getLogger("llm_gateway")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _error(status: int, message: str, etype: str = "gateway_error") -> JSONResponse:
    return JSONResponse({"error": {"type": etype, "message": message}}, status_code=status)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _with_query(url: str, request: Request) -> str:
    return f"{url}?{request.url.query}" if request.url.query else url


class Gateway:
    def __init__(
        self,
        settings: Settings,
        *,
        store: RequestStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        counter: TokenCounter | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or RequestStore(settings.db_path)
        self.counter = counter or TokenCounter(settings.tokenizer)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_s, connect=10.0),
            transport=transport,
        )
        self._tasks: set[asyncio.Task] = set()

    async def drain(self) -> None:
        """Wait until every pending log write has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        await self.drain()
        await self.client.aclose()
        self.store.close()

    # ---------------------------------------------------------------- auth

    def tenant(self, request: Request) -> str:
        keys = self.settings.gateway_keys
        if keys:
            given = request.headers.get("x-gateway-key", "").encode()
            for key, tenant in keys.items():
                if given and hmac.compare_digest(given, key.encode()):
                    return tenant
            raise HTTPException(status_code=401, detail="missing or invalid X-Gateway-Key")
        return request.headers.get("x-gateway-tenant") or "default"

    # ------------------------------------------------------------ LLM calls

    async def handle_llm(self, request: Request, api: ApiType, path_after_v1: str) -> Response:
        started = time.perf_counter()
        tenant = self.tenant(request)
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _error(400, "request body must be a JSON object", "invalid_request_error")

        model = body.get("model") if isinstance(body.get("model"), str) else None
        try:
            provider = resolve_provider(self.settings, api, model, request.headers.get("x-gateway-provider"))
            headers = build_upstream_headers(provider, request.headers)
        except RoutingError as exc:
            return _error(400, str(exc), "invalid_request_error")
        except GatewayConfigError as exc:
            log.error("%s", exc)
            return _error(500, str(exc))

        stream = bool(body.get("stream"))
        content = raw
        stream_options = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
        inject_usage = (
            api == "openai"
            and stream
            and self.settings.inject_stream_usage
            and provider.supports_stream_usage
            and not stream_options.get("include_usage")
        )
        if inject_usage:
            patched = {**body, "stream_options": {**stream_options, "include_usage": True}}
            content = json.dumps(patched, ensure_ascii=False).encode("utf-8")

        request_id = uuid.uuid4().hex
        record: dict[str, Any] = {
            "id": request_id,
            "ts": now_iso(),
            "tenant": tenant,
            "app": request.headers.get("x-gateway-app"),
            "api": api,
            "path": f"/v1/{path_after_v1}",
            "provider": provider.name,
            "model": model,
            "stream": int(stream),
        }
        # Analysis runs in a worker thread while the upstream call is in flight.
        analysis = asyncio.create_task(asyncio.to_thread(analyze_request, body, api, self.counter))
        url = _with_query(upstream_url(provider, path_after_v1), request)

        if stream:
            return await self._forward_stream(url, content, headers, provider, record, analysis, started, inject_usage)
        return await self._forward_json(url, content, headers, provider, record, analysis, started)

    async def _forward_json(
        self,
        url: str,
        content: bytes,
        headers: dict[str, str],
        provider: ProviderConfig,
        record: dict[str, Any],
        analysis: asyncio.Task,
        started: float,
    ) -> Response:
        try:
            resp = await self.client.post(url, content=content, headers=headers)
        except httpx.HTTPError as exc:
            record.update(status_code=502, error=type(exc).__name__, completed=0, latency_ms=_elapsed_ms(started))
            self._schedule_finalize(record, analysis, ResponseInfo())
            return _error(502, f"upstream request failed: {type(exc).__name__}")

        record.update(status_code=resp.status_code, completed=1, latency_ms=_elapsed_ms(started))
        try:
            info = info_from_json(resp.json(), provider.type)
        except ValueError:
            info = ResponseInfo()
        self._schedule_finalize(record, analysis, info)

        out_headers = filter_response_headers(resp.headers)
        out_headers["x-gateway-request-id"] = record["id"]
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)

    async def _forward_stream(
        self,
        url: str,
        content: bytes,
        headers: dict[str, str],
        provider: ProviderConfig,
        record: dict[str, Any],
        analysis: asyncio.Task,
        started: float,
        strip_usage: bool,
    ) -> Response:
        req = self.client.build_request("POST", url, content=content, headers=headers)
        try:
            resp = await self.client.send(req, stream=True)
        except httpx.HTTPError as exc:
            record.update(status_code=502, error=type(exc).__name__, completed=0, latency_ms=_elapsed_ms(started))
            self._schedule_finalize(record, analysis, ResponseInfo())
            return _error(502, f"upstream request failed: {type(exc).__name__}")

        out_headers = filter_response_headers(resp.headers)
        out_headers["x-gateway-request-id"] = record["id"]

        if resp.status_code >= 400 or "text/event-stream" not in resp.headers.get("content-type", ""):
            data = await resp.aread()
            await resp.aclose()
            record.update(status_code=resp.status_code, completed=1, latency_ms=_elapsed_ms(started))
            try:
                info = info_from_json(json.loads(data), provider.type)
            except ValueError:
                info = ResponseInfo()
            self._schedule_finalize(record, analysis, info)
            return Response(content=data, status_code=resp.status_code, headers=out_headers)

        tracker = StreamTracker(provider.type, strip_usage_only_chunks=strip_usage)

        async def relay():
            buffer = b""
            completed = False
            ttft: float | None = None
            try:
                async for chunk in resp.aiter_bytes():
                    if ttft is None:
                        ttft = _elapsed_ms(started)
                    # A lone trailing \r stays in the buffer until its \n arrives.
                    buffer = (buffer + chunk).replace(b"\r\n", b"\n")
                    while (idx := buffer.find(b"\n\n")) != -1:
                        event, buffer = buffer[:idx], buffer[idx + 2 :]
                        if tracker.feed(event):
                            yield event + b"\n\n"
                if buffer.strip() and tracker.feed(buffer):
                    yield buffer
                completed = True
            finally:
                record.update(
                    status_code=resp.status_code,
                    completed=int(completed),
                    latency_ms=_elapsed_ms(started),
                    ttft_ms=ttft,
                )
                if not completed:
                    record.setdefault("error", "client_disconnected_or_stream_error")
                # Scheduling never awaits, so it is safe even when the stream is cancelled.
                self._schedule_finalize(record, analysis, tracker.info(), output_text=tracker.output_text)
                await resp.aclose()

        return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers)

    # ------------------------------------------------------------ passthrough

    async def handle_passthrough(self, request: Request, path: str) -> Response:
        started = time.perf_counter()
        tenant = self.tenant(request)
        override = request.headers.get("x-gateway-provider")
        try:
            if override:
                provider = self.settings.provider(override)
                if provider is None:
                    raise RoutingError(f"unknown provider '{override}'")
            else:
                api: ApiType = "anthropic" if path.startswith("messages") else "openai"
                provider = resolve_provider(self.settings, api, None)
            headers = build_upstream_headers(provider, request.headers, passthrough=True)
        except RoutingError as exc:
            return _error(400, str(exc), "invalid_request_error")
        except GatewayConfigError as exc:
            log.error("%s", exc)
            return _error(500, str(exc))

        record: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "ts": now_iso(),
            "tenant": tenant,
            "app": request.headers.get("x-gateway-app"),
            "api": "passthrough",
            "path": f"/v1/{path}",
            "provider": provider.name,
        }
        body = await request.body()
        req = self.client.build_request(
            request.method,
            _with_query(upstream_url(provider, path), request),
            content=body or None,
            headers=headers,
        )
        try:
            resp = await self.client.send(req, stream=True)
        except httpx.HTTPError as exc:
            record.update(status_code=502, error=type(exc).__name__, completed=0, latency_ms=_elapsed_ms(started))
            self._schedule_finalize(record, None, None)
            return _error(502, f"upstream request failed: {type(exc).__name__}")

        out_headers = filter_response_headers(resp.headers)
        out_headers["x-gateway-request-id"] = record["id"]

        async def relay():
            completed = False
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                completed = True
            finally:
                record.update(status_code=resp.status_code, completed=int(completed), latency_ms=_elapsed_ms(started))
                self._schedule_finalize(record, None, None)
                await resp.aclose()

        return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers)

    # ---------------------------------------------------------------- logging

    def _schedule_finalize(
        self,
        record: dict[str, Any],
        analysis: asyncio.Task | None,
        info: ResponseInfo | None,
        output_text: str | None = None,
    ) -> None:
        task = asyncio.get_running_loop().create_task(self._finalize(record, analysis, info, output_text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _finalize(
        self,
        record: dict[str, Any],
        analysis_task: asyncio.Task | None,
        info: ResponseInfo | None,
        output_text: str | None,
    ) -> None:
        try:
            analysis: RequestAnalysis | None = None
            if analysis_task is not None:
                try:
                    analysis = await analysis_task
                    record.update(analysis.as_record())
                except Exception:
                    log.exception("request analysis failed")

            info = info or ResponseInfo()
            usage = info.usage
            succeeded = 200 <= (record.get("status_code") or 0) < 300
            if usage is None and succeeded and output_text is not None and analysis is not None:
                # Provider sent no usage (e.g. a local server); estimate both sides.
                output = await asyncio.to_thread(self.counter.count, output_text)
                usage = Usage(input_total=analysis.est_input_tokens, output=output, source="estimated")

            if usage is not None:
                record.update(
                    input_tokens=usage.input_total,
                    output_tokens=usage.output,
                    cached_input_tokens=usage.cached_input,
                    cache_write_tokens=usage.cache_write,
                    reasoning_tokens=usage.reasoning,
                    usage_source=usage.source,
                )
            record["response_model"] = info.response_model
            record["finish_reason"] = str(info.finish_reason) if info.finish_reason else None
            if info.error_type and not record.get("error"):
                record["error"] = info.error_type

            price = find_price(self.settings.pricing, record.get("model"), info.response_model)
            record["cost_usd"], record["cost_output_usd"] = compute_cost(price, usage)
            await asyncio.to_thread(self.store.insert, record)
        except Exception:
            log.exception("failed to record request %s", record.get("id"))

    # ---------------------------------------------------------------- stats

    async def stats(self, request: Request, days: float) -> dict[str, Any]:
        tenant = self.tenant(request)
        rows = await asyncio.to_thread(self.store.fetch, since_iso(days))
        # With gateway keys, each tenant only sees its own traffic.
        scope = tenant if self.settings.gateway_keys else None
        return build_report(rows, days=days, tenant=scope)


def create_app(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    store: RequestStore | None = None,
) -> FastAPI:
    gateway = Gateway(settings, store=store, transport=transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await gateway.aclose()

    app = FastAPI(title="LLM Gateway", version=__version__, lifespan=lifespan)
    app.state.gateway = gateway

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await gateway.handle_llm(request, "openai", "chat/completions")

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        return await gateway.handle_llm(request, "anthropic", "messages")

    @app.get("/gateway/stats")
    async def stats(request: Request, days: float = 7.0) -> dict[str, Any]:
        return await gateway.stats(request, days)

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def passthrough(path: str, request: Request) -> Response:
        return await gateway.handle_passthrough(request, path)

    return app
