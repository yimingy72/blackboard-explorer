"""Normalize provider usage and price each model request independently."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bbx_contracts.billing import token_cost
from bbx_contracts.models import Price, Usage


def _separate_cache_input(provider: str | None) -> bool:
    return bool(provider and (provider.startswith("anthropic") or provider == "bedrock"))


def _input_tokens(details: Mapping[str, Any] | None, provider: str | None = None) -> int:
    details = details or {}
    total = int(details.get("input_token_count") or 0)
    if _separate_cache_input(provider):
        total += int(details.get("cache_creation_input_token_count") or 0)
        total += int(details.get("cache_read_input_token_count") or 0)
    return total


def _usage_known(details: Mapping[str, Any] | None, provider: str | None = None) -> bool:
    if not details or details.get("output_token_count") is None:
        return False
    return details.get("input_token_count") is not None or (
        not _separate_cache_input(provider)
        and details.get("prompt_cache_hit_tokens") is not None
        and details.get("prompt_cache_miss_tokens") is not None
    )


def _token_usage(details: Mapping[str, Any] | None, provider: str | None = None) -> Usage:
    details = details or {}
    total_input = int(details.get("input_token_count") or 0)
    if _separate_cache_input(provider):
        hit = int(details.get("cache_read_input_token_count") or 0)
        miss = total_input + int(details.get("cache_creation_input_token_count") or 0)
    else:
        raw_hit = details.get("prompt_cache_hit_tokens")
        hit = int(
            raw_hit if raw_hit is not None else details.get("cache_read_input_token_count") or 0
        )
        raw_miss = details.get("prompt_cache_miss_tokens")
        miss = int(raw_miss if raw_miss is not None else max(0, total_input - hit))
    output = int(details.get("output_token_count") or 0)
    reasoning = int(details.get("reasoning_output_token_count") or 0)
    return Usage(
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        output_tokens=output,
        reasoning_tokens=reasoning,
    )


def _usage(
    details: Mapping[str, Any] | None, price: Price, provider: str | None = None
) -> tuple[Usage, str | None]:
    usage = _token_usage(details, provider)
    usage.cost, warning = token_cost(usage, price)
    return usage, warning
