"""Provider routing and header handling."""

from __future__ import annotations

import os
from typing import Mapping

from .config import ApiType, ProviderConfig, Settings


class RoutingError(Exception):
    """The request cannot be routed. Maps to HTTP 400."""


class GatewayConfigError(Exception):
    """The gateway is misconfigured. Maps to HTTP 500."""


_FORWARD = {
    "openai": {"authorization", "openai-organization", "openai-project", "openai-beta"},
    "anthropic": {"x-api-key", "authorization", "anthropic-version", "anthropic-beta"},
}

# Headers that must not be copied from the upstream response to the client.
_HOP_BY_HOP = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "date",
    "server",
}


def resolve_provider(
    settings: Settings, api: ApiType, model: str | None, override: str | None = None
) -> ProviderConfig:
    if override:
        p = settings.provider(override)
        if p is None:
            raise RoutingError(f"unknown provider '{override}'")
        if p.type != api:
            raise RoutingError(f"provider '{override}' does not serve the {api} API")
        return p

    candidates = [p for p in settings.providers if p.type == api]
    if model:
        best, best_len = None, -1
        for p in candidates:
            for prefix in p.models:
                if model.startswith(prefix) and len(prefix) > best_len:
                    best, best_len = p, len(prefix)
        if best is not None:
            return best

    default = settings.provider(settings.default_provider)
    if default is not None and default.type == api:
        return default
    if candidates:
        return candidates[0]
    raise RoutingError(f"no provider configured for the {api} API")


def upstream_url(provider: ProviderConfig, path_after_v1: str) -> str:
    return provider.base_url.rstrip("/") + "/" + path_after_v1.lstrip("/")


def build_upstream_headers(
    provider: ProviderConfig, incoming: Mapping[str, str], *, passthrough: bool = False
) -> dict[str, str]:
    allowed = set(_FORWARD[provider.type])
    if passthrough:
        allowed |= {"content-type", "accept"}
    headers = {k.lower(): v for k, v in incoming.items() if k.lower() in allowed}

    if provider.api_key_env:
        key = os.environ.get(provider.api_key_env)
        if not key:
            raise GatewayConfigError(
                f"provider '{provider.name}' expects environment variable {provider.api_key_env}"
            )
        headers.pop("authorization", None)
        headers.pop("x-api-key", None)
        if provider.type == "openai":
            headers["authorization"] = f"Bearer {key}"
        else:
            headers["x-api-key"] = key

    if provider.type == "anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")
    if not passthrough:
        headers["content-type"] = "application/json"
    return headers


def filter_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}
