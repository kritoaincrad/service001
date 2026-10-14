"""Gateway configuration, loaded from a YAML file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

ApiType = Literal["openai", "anthropic"]


class ProviderConfig(BaseModel):
    """An upstream LLM provider.

    base_url includes the API version prefix, e.g. https://api.openai.com/v1 or
    https://api.anthropic.com/v1. The gateway appends the path that follows /v1.
    """

    name: str
    type: ApiType
    base_url: str
    # When set, the gateway injects this key and ignores the key sent by the client.
    # When empty, the client key is passed through unchanged.
    api_key_env: str | None = None
    # Model name prefixes routed to this provider. The longest match wins.
    models: list[str] = Field(default_factory=list)
    # Whether the provider accepts stream_options.include_usage (OpenAI API only).
    supports_stream_usage: bool = True


class ModelPrice(BaseModel):
    """Prices in USD per one million tokens."""

    input: float
    output: float
    cached_input: float | None = None  # falls back to the input price
    cache_write: float | None = None  # falls back to the input price


class Settings(BaseModel):
    providers: list[ProviderConfig]
    default_provider: str | None = None
    db_path: str = "data/gateway.db"
    tokenizer: str = "o200k_base"
    inject_stream_usage: bool = True
    request_timeout_s: float = 600.0
    # Gateway key -> tenant name. Empty means the gateway does not authenticate clients.
    gateway_keys: dict[str, str] = Field(default_factory=dict)
    # Model name or prefix -> price. No prices ship by default.
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if not self.providers:
            raise ValueError("at least one provider must be configured")
        names = [p.name for p in self.providers]
        if len(set(names)) != len(names):
            raise ValueError("provider names must be unique")
        if self.default_provider and self.default_provider not in names:
            raise ValueError(f"default_provider '{self.default_provider}' is not a configured provider")
        return self

    def provider(self, name: str | None) -> ProviderConfig | None:
        for p in self.providers:
            if p.name == name:
                return p
        return None


def load_settings(path: str | Path | None = None) -> Settings:
    cfg_path = Path(path or os.environ.get("GATEWAY_CONFIG", "config.yaml"))
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)
