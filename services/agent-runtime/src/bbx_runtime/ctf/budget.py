"""Conservative per-model-call budget bounds for CTF admission."""

from datetime import UTC, datetime
from decimal import Decimal

from bbx_contracts.billing import effective_price
from bbx_contracts.models import ModelConfig

CTF_MAX_OUTPUT_TOKENS = 8192


def ctf_max_output_tokens(model: ModelConfig) -> int:
    """Share the exact model output bound between requests and budget admission."""
    # DeepSeek's official maximum includes reasoning tokens (checked 2026-10-09):
    # https://api-docs.deepseek.com/api/create-chat-completion/
    # Compatible transports can expose the same exact model ID.
    if model.model in {
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "deepseek-v4-pro",
    }:
        return 393216
    return CTF_MAX_OUTPUT_TOKENS


def ctf_call_reservation(
    model: ModelConfig, *, context_tokens: int, max_output_tokens: int | None = None
) -> Decimal | None:
    """Return a cost bound only when both token and price bounds are explicit.

    A missing context window or incomplete provider price table is deliberately
    non-hard-cap: the caller must not invent a reservation from an estimate.
    """
    if max_output_tokens is None:
        max_output_tokens = ctf_max_output_tokens(model)
    if model.context_window is None or context_tokens <= 0 or max_output_tokens <= 0:
        return None
    price, _ = effective_price(model, datetime.now(UTC))
    rates = (price.cache_hit_per_m, price.cache_miss_per_m, price.output_per_m)
    if price.currency is None or any(rate is None for rate in rates):
        return None
    assert price.cache_hit_per_m is not None
    assert price.cache_miss_per_m is not None
    assert price.output_per_m is not None
    input_rate = max(price.cache_hit_per_m, price.cache_miss_per_m)
    input_tokens = min(context_tokens, model.context_window)
    return (
        Decimal(input_tokens) * input_rate + Decimal(max_output_tokens) * price.output_per_m
    ) / Decimal(1_000_000)
