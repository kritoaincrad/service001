"""Cost calculation from configured prices. No prices are hardcoded."""

from __future__ import annotations

from .config import ModelPrice
from .usage import Usage


def find_price(pricing: dict[str, ModelPrice], *models: str | None) -> ModelPrice | None:
    """Exact match first, then the longest configured prefix, for each candidate name."""
    for model in models:
        if not model:
            continue
        if model in pricing:
            return pricing[model]
        best, best_len = None, -1
        for key, price in pricing.items():
            if model.startswith(key) and len(key) > best_len:
                best, best_len = price, len(key)
        if best is not None:
            return best
    return None


def compute_cost(price: ModelPrice | None, usage: Usage | None) -> tuple[float | None, float | None]:
    """Return (total cost, output part of the cost) in USD."""
    if price is None or usage is None or usage.input_total is None:
        return None, None
    cached = usage.cached_input
    written = usage.cache_write
    uncached = max(usage.input_total - cached - written, 0)
    cached_rate = price.cached_input if price.cached_input is not None else price.input
    write_rate = price.cache_write if price.cache_write is not None else price.input
    input_cost = uncached * price.input + cached * cached_rate + written * write_rate
    output_cost = (usage.output or 0) * price.output
    return (input_cost + output_cost) / 1_000_000, output_cost / 1_000_000
