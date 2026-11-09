"""Native gateway identity, Task key publication and cost evidence contracts."""

from datetime import datetime
from decimal import ROUND_DOWN, Decimal, DecimalException
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import ContractModel
from .runner import Digest, EffectiveRunnerModel, RunnerFence, RunnerId

TRUSTED_ATTRIBUTE_HEADERS = {
    "bbx_task_id": "x-bbx-task-id",
    "bbx_binding_id": "x-bbx-binding-id",
    "bbx_epoch": "x-bbx-epoch",
    "bbx_agent_id": "x-bbx-agent-id",
    "bbx_attempt_id": "x-bbx-attempt-id",
    "bbx_call_id": "x-bbx-call-id",
    "bbx_sdk_session_id": "x-bbx-sdk-session-id",
}


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


class NativeBudgetBinding(ContractModel):
    task_id: str
    accounting_source_id: str
    key_name: str
    budget_name: str
    key_fingerprint: Digest
    native_resource_id: str | None = None
    threshold_usd: Decimal
    exchange_rate: NativeBudgetExchange = None
    selected_models: list[str]
    publication_state: Literal["prepared", "creating", "creating_unknown", "ready", "retired"]
    budget_currency: NativeBudgetCurrency = None

    @model_validator(mode="after")
    def exact_currency(self):
        require_native_budget_currency(self.exchange_rate, self.budget_currency)
        return self


class NativeModelSelection(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    accounting_source_id: str
    model_alias: str
    sdk_model: EffectiveRunnerModel
    catalog_revision: Digest


class NativeFeeView(ContractModel):
    accounting_source_id: str
    currency: Literal["USD"] = "USD"
    confirmed_usd: Decimal
    converted_cny: Decimal | None = Field(default=None, exclude_if=lambda value: value is None)
    threshold_usd: Decimal
    overage_usd: Decimal
    pending_attempts: int
    hold_reason: str | None = None
    exchange_rate: NativeBudgetExchange = None
    catalog_revision: str
    native_used_high_water_usd: Decimal | None = None
    budget_currency: NativeBudgetCurrency = None

    @model_validator(mode="after")
    def exact_currency(self):
        require_native_budget_currency(self.exchange_rate, self.budget_currency)
        return self


class NativeModelDispatchRequest(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    fence: RunnerFence
    binding_id: RunnerId
    sdk_session_id: RunnerId
    call_id: RunnerId
    attempt_id: RunnerId
    body_digest: Digest


class NativeModelDispatchGrant(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    send_allowed: bool
    attempt_id: RunnerId
    state: str
    body_digest: Digest
    dispatch_id: RunnerId | None = None
    native_gateway_url: str | None = None
    task_gateway_key: str | None = Field(default=None, repr=False)
    accounting_source_id: str | None = None
    trusted_attributes: dict[str, str] = Field(default_factory=dict)
    authorized_wait_until_ms: int | None = None


class NativeTransportObservation(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    attempt_id: RunnerId
    dispatch_id: RunnerId
    http_status: int | None = Field(default=None, ge=100, le=599)
    phase: Literal["response_ended", "disconnected", "not_sent"]


class NativeTransportReceipt(ContractModel):
    protocol: Literal["bbx.task-runner.v1"]
    confirmed: Literal[True]
    attempt_id: RunnerId
    dispatch_id: RunnerId
    request_digest: Digest


class GatewayCredentialsRequest(ContractModel):
    expected_epoch: int = Field(ge=1)
