"""Conservative per-model-call budget bounds for CTF admission."""

from datetime import UTC, datetime
from decimal import Decimal

from bbx_contracts.billing import effective_price
from bbx_contracts.model_limits import effective_max_input_tokens, effective_max_output_tokens
from bbx_contracts.models import ModelConfig


def ctf_max_output_tokens(model: ModelConfig) -> int:
    """Share the exact model output bound between requests and budget admission."""
    return effective_max_output_tokens(model)


def ctf_call_reservation(model: ModelConfig, *, context_tokens: int) -> Decimal | None:
    """Return a cost bound only when both token and price bounds are explicit.

    A missing input bound or incomplete provider price table is deliberately
    non-hard-cap: the caller must not invent a reservation from an estimate.
    """
    max_output_tokens = ctf_max_output_tokens(model)
    input_limit = effective_max_input_tokens(model)
    if input_limit is None or context_tokens <= 0:
        return None
    price, _ = effective_price(model, datetime.now(UTC))
    rates = (price.cache_hit_per_m, price.cache_miss_per_m, price.output_per_m)
    if price.currency is None or any(rate is None for rate in rates):
        return None
    assert price.cache_hit_per_m is not None
    assert price.cache_miss_per_m is not None
    assert price.output_per_m is not None
    input_rate = max(price.cache_hit_per_m, price.cache_miss_per_m)
    input_tokens = min(context_tokens, input_limit)
    return (
        Decimal(input_tokens) * input_rate + Decimal(max_output_tokens) * price.output_per_m
    ) / Decimal(1_000_000)
