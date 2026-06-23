"""Approximate token pricing for the cost circuit breaker and dashboard.

Prices are USD per 1M tokens (input, output). This is an estimate for budgeting/
guardrails — exact billing comes from the provider. Unknown models fall back to a
conservative default. The active model can be priced from the admin AI config later.
"""

from __future__ import annotations

# model substring -> (input_per_1m, output_per_1m)
_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "qwen-turbo": (0.05, 0.20),
    "qwen-plus": (0.40, 1.20),
    "qwen-max": (1.60, 6.40),
    "glm-4-flash": (0.01, 0.01),
    "glm-4-air": (0.07, 0.07),
    "glm-4-plus": (0.70, 0.70),
    "moonshot-v1-8k": (1.70, 1.70),
}

_DEFAULT = (1.00, 3.00)


def price_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in _PRICES.items():
        if key in m:
            return price
    return _DEFAULT


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = price_for(model)
    return (prompt_tokens / 1_000_000) * pin + (completion_tokens / 1_000_000) * pout
