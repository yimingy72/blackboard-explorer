"""Money stays exact and the selected request window is reproducible."""

from datetime import datetime
from decimal import Decimal

import pytest
from bbx_contracts.billing import add_usage, effective_price, token_cost
from bbx_contracts.models import ModelConfig, Price, Usage


def model(**price) -> ModelConfig:
    return ModelConfig(
        provider="deepseek",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        reasoning_effort="high",
        price=Price(
            currency="CNY",
            cache_hit_per_m=Decimal("0.04"),
            cache_miss_per_m=Decimal("2"),
            output_per_m=Decimal("8"),
            off_peak=False,
            **price,
        ),
    )


@pytest.mark.parametrize(
    "stamp, factor",
    [
        ("2026-09-28T08:59:59+08:00", "0.5"),
        ("2026-09-28T09:00:00+08:00", "1"),
        ("2026-09-28T12:00:00+08:00", "0.5"),
        ("2026-09-28T13:30:00+08:00", "0.5"),
        ("2026-09-28T14:00:00+08:00", "1"),
        ("2026-09-28T18:00:00+08:00", "0.5"),
        ("2026-09-25T10:00:00+08:00", "0.5"),
        ("2026-10-01T10:00:00+08:00", "0.5"),
        ("2026-10-10T10:00:00+08:00", "0.5"),  # Saturday remains off-peak despite makeup workday.
    ],
)
def test_official_windows(stamp, factor):
    result, audit = effective_price(
        model(billing_mode="deepseek_schedule"), datetime.fromisoformat(stamp)
    )
    assert result.output_per_m == Decimal(8) * Decimal(factor)
    assert audit["factor"] == factor


def test_fixed_legacy_and_unknown_calendar_are_explicit():
    stamp = datetime.fromisoformat("2026-09-28T13:30:00+08:00")
    result, audit = effective_price(model(), stamp)
    assert result.output_per_m == 8 and audit["mode"] == "fixed"
    result, audit = effective_price(
        model(billing_mode="deepseek_schedule"), datetime.fromisoformat("2027-01-04T10:00:00+08:00")
    )
    assert result.output_per_m == 8 and "warning" in audit


def test_off_peak_input_rate_is_not_discounted_twice():
    configured = model(billing_mode="deepseek_schedule")
    configured.price = configured.price.model_copy(
        update={"off_peak": True, "output_per_m": Decimal(4)}
    )
    peak, _ = effective_price(configured, datetime.fromisoformat("2026-09-28T10:00:00+08:00"))
    off, _ = effective_price(configured, datetime.fromisoformat("2026-09-28T13:00:00+08:00"))
    assert peak.output_per_m == 8 and off.output_per_m == 4
    cost, _ = token_cost(Usage(output_tokens=1_000_000, reasoning_tokens=900_000), off)
    assert cost == 4  # Reasoning is already included in output.


def test_money_sums_are_decimal_and_third_party_cannot_claim_discount():
    value = {}
    for _ in range(100):
        value = add_usage(value, {"cost": "0.00000001", "output_tokens": 1})
    assert Decimal(value["cost"]) == Decimal("0.000001")
    assert value["output_tokens"] == 100
    with pytest.raises(ValueError, match="DeepSeek"):
        ModelConfig.model_validate(
            {
                **model().model_dump(),
                "base_url": "https://api.deepseek.com.other.example",
                "price": {**model().price.model_dump(), "billing_mode": "deepseek_schedule"},
            }
        )
    with pytest.raises(ValueError, match="timezone"):
        effective_price(model(), datetime(2026, 9, 28))
