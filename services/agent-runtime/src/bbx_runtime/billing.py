"""Normalize provider usage and price each model request independently."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from agent_framework import ChatContext, ChatMiddleware
from bbx_contracts.billing import effective_price, token_cost
from bbx_contracts.models import ModelConfig, Price, Usage


def _separate_cache_input(provider: str | None) -> bool:
    return bool(provider and (provider.startswith("anthropic") or provider == "bedrock"))


def _input_tokens(details: Mapping[str, Any] | None, provider: str | None = None) -> int:
    details = details or {}
    total = int(details.get("input_token_count") or 0)
    if _separate_cache_input(provider):
        total += int(details.get("cache_creation_input_token_count") or 0)
        total += int(details.get("cache_read_input_token_count") or 0)
    return total


def _usage(
    details: Mapping[str, Any] | None, price: Price, provider: str | None = None
) -> tuple[Usage, str | None]:
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
    usage = Usage(
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        output_tokens=output,
        reasoning_tokens=reasoning,
    )
    usage.cost, warning = token_cost(usage, price)
    return usage, warning


class ReviewBillingMiddleware(ChatMiddleware):
    def __init__(
        self, checkpoint: Any, model: ModelConfig, message_id: str, mode: str | None = None
    ) -> None:
        self.checkpoint, self.model, self.message_id, self.mode = (
            checkpoint,
            model,
            message_id,
            mode,
        )

    async def process(self, context: ChatContext, call_next: Any) -> None:
        price, pricing = effective_price(self.model, datetime.now(UTC), mode_override=self.mode)
        # HistoryProvider saves these rates with the response and its usage atomically.
        self.checkpoint.review_request = {
            "message_id": self.message_id,
            "price": price,
            "pricing": pricing,
            "provider": self.model.provider,
        }
        await call_next()
