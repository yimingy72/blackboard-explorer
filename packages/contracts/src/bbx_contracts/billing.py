"""Auditable token estimates; provider invoices remain the source of actual charges."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bbx_contracts.models import ModelConfig, Price, Usage

PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
SCHEDULE_VERSION = "deepseek-2026-09-28"
# State Council notice: https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm
HOLIDAYS_2026 = (
    ("01-01", "01-03"),
    ("02-15", "02-23"),
    ("04-04", "04-06"),
    ("05-01", "05-05"),
    ("06-19", "06-21"),
    ("09-25", "09-27"),
    ("10-01", "10-07"),
)
DEEPSEEK_MODELS = {
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4-pro",
}


def supports_deepseek_schedule(base_url: str, model: str) -> bool:
    parsed = urlsplit(base_url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "api.deepseek.com"
        and model in DEEPSEEK_MODELS
    )


def effective_price(
    model: ModelConfig, requested_at: datetime, *, mode_override: str | None = None
) -> tuple[Price, dict[str, Any]]:
    """Use request start time, never tool completion time, for the rate window."""
    if requested_at.tzinfo is None:
        raise ValueError("A timezone-aware request timestamp is required")
    price = model.price
    mode = mode_override or price.billing_mode
    factor = Decimal(1)
    metadata: dict[str, Any] = {
        "mode": mode,
        "requested_at": requested_at.astimezone(UTC).isoformat(),
        "currency": price.currency,
        "estimated": True,
    }
    if mode == "deepseek_schedule":
        if not supports_deepseek_schedule(model.base_url, model.model):
            raise ValueError("Peak/off-peak billing requires an official DeepSeek model endpoint")
        local = requested_at.astimezone(ZoneInfo("Asia/Shanghai"))
        holiday = local.year == 2026 and any(
            start <= local.strftime("%m-%d") <= end for start, end in HOLIDAYS_2026
        )
        peak = (
            local.weekday() < 5 and not holiday and (9 <= local.hour < 12 or 14 <= local.hour < 18)
        )
        if local.date() < date(2026, 9, 10):
            metadata["warning"] = "当前时段规则不追溯到 2026-09-10 前，保留录入费率"
        else:
            target = Decimal(1) if peak else Decimal("0.5")
            baseline = Decimal("0.5") if price.off_peak else Decimal(1)
            factor = target / baseline
            metadata["period"] = "peak" if peak else "off_peak"
            if local.year != 2026 and peak:
                metadata["warning"] = "该年度节假日日历未更新，工作日峰时按高峰保守估算"
        metadata.update(schedule_version=SCHEDULE_VERSION, source=PRICING_SOURCE)
    result = price.model_copy(
        update={
            key: value * factor if value is not None else None
            for key, value in (
                ("cache_hit_per_m", price.cache_hit_per_m),
                ("cache_miss_per_m", price.cache_miss_per_m),
                ("output_per_m", price.output_per_m),
            )
        }
    )
    metadata.update(factor=str(factor), rates=result.model_dump(mode="json"))
    return result, metadata


def token_cost(usage: Usage, price: Price) -> tuple[Decimal, str | None]:
    if any(
        value is None
        for value in (price.cache_hit_per_m, price.cache_miss_per_m, price.output_per_m)
    ):
        return Decimal(0), "价格未配置"
    assert price.cache_hit_per_m is not None
    assert price.cache_miss_per_m is not None
    assert price.output_per_m is not None
    return (
        Decimal(usage.cache_hit_tokens) * price.cache_hit_per_m
        + Decimal(usage.cache_miss_tokens) * price.cache_miss_per_m
        + Decimal(usage.output_tokens) * price.output_per_m
    ) / Decimal(1_000_000), None


def add_usage(previous: Mapping[str, Any], delta: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize money as decimal text; JSON floats corrupt repeated accumulation."""
    result: dict[str, Any] = {
        key: int(previous.get(key) or 0) + int(delta.get(key) or 0)
        for key in previous.keys() | delta.keys()
        if key != "cost"
    }
    if "cost" in previous or "cost" in delta:
        result["cost"] = str(
            Decimal(str(previous.get("cost") or 0)) + Decimal(str(delta.get("cost") or 0))
        )
    return result
