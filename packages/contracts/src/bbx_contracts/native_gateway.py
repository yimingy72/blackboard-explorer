"""Native gateway identity, Task key publication and cost evidence contracts."""

from datetime import datetime
from decimal import ROUND_DOWN, Decimal, DecimalException
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import ContractModel


class FrozenExchangeRate(ContractModel):
    rate_cny_per_usd: Decimal = Field(gt=0)
    source_ref: str = Field(min_length=1, max_length=2000)
    observed_at: datetime
    rounding: Literal["ROUND_DOWN"] = "ROUND_DOWN"
    usd_decimal_places: Literal[9] = 9


MAX_NATIVE_USD = Decimal("9223372036.854775807")


def native_usd_amount(amount: Decimal) -> Decimal:
    """Keep the native USD threshold exact at its original wire precision."""
    try:
        usd = amount.quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)
    except DecimalException as error:
        raise ValueError("Task budget cannot be represented in native USD") from error
    if not usd.is_finite() or usd <= 0:
        raise ValueError("USD threshold must be positive")
    if usd != amount or usd > MAX_NATIVE_USD or Decimal(str(float(usd))) != usd:
        raise ValueError("Task budget cannot be represented in native USD")
    return usd


def native_budget_threshold_usd(cny: Decimal, rate: Decimal) -> Decimal:
    try:
        usd = (cny / rate).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)
    except DecimalException as error:
        raise ValueError("Task budget cannot be represented in USD") from error
    if not usd.is_finite() or usd <= 0:
        raise ValueError("Converted USD threshold must be positive")
    return native_usd_amount(usd)


NativeBudgetExchange = Annotated[
    FrozenExchangeRate | None, Field(exclude_if=lambda value: value is None)
]
NativeBudgetCurrency = Annotated[
    Literal["USD"] | None, Field(exclude_if=lambda value: value is None)
]


def require_native_budget_currency(
    exchange_rate: FrozenExchangeRate | None, budget_currency: Literal["USD"] | None
) -> None:
    if (exchange_rate is None) != (budget_currency == "USD"):
        raise ValueError("Specify direct USD or the original frozen exchange rate")


class NativeBudgetPrepare(ContractModel):
    expected_epoch: int = Field(ge=1)
    accounting_source_id: str = Field(min_length=1, max_length=200)
    selected_models: list[str] = Field(min_length=1, max_length=20)
    exchange_rate: NativeBudgetExchange = None
    budget_currency: NativeBudgetCurrency = None

    @model_validator(mode="after")
    def exact_currency(self):
        require_native_budget_currency(self.exchange_rate, self.budget_currency)
        return self


class GatewayCredentialsRequest(ContractModel):
    expected_epoch: int = Field(ge=1)
