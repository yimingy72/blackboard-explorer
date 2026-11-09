from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from bbx_contracts.native_gateway import (
    FrozenExchangeRate,
    NativeBudgetPrepare,
    native_budget_threshold_usd,
    native_usd_amount,
)
from bbx_contracts.runner_bootstrap import NativeBudgetIncreaseRequest, NativeRunnerLaunch
from pydantic import ValidationError


def test_usd_budget_has_no_exchange_rate_and_keeps_exact_native_precision():
    assert native_usd_amount(Decimal("0.009")) == Decimal("0.009")
    assert native_usd_amount(Decimal("0.000000001")) == Decimal("0.000000001")
    for invalid in ("0", "NaN", "0.0000000009", "9223372036.854775807"):
        with pytest.raises(ValueError):
            native_usd_amount(Decimal(invalid))
    prepare = NativeBudgetPrepare(
        expected_epoch=1,
        accounting_source_id="fixture",
        selected_models=["model"],
        budget_currency="USD",
    )
    assert prepare.model_dump(mode="json") == {
        "expected_epoch": 1,
        "accounting_source_id": "fixture",
        "selected_models": ["model"],
        "budget_currency": "USD",
    }


def test_original_exchange_and_direct_usd_launch_are_mutually_exclusive():
    identity = {"request_id": str(uuid4()), "model_alias": "model"}
    exchange = FrozenExchangeRate(
        rate_cny_per_usd=Decimal("7"),
        source_ref="historical evidence",
        observed_at=datetime(2026, 11, 6, tzinfo=UTC),
    )
    legacy = NativeRunnerLaunch.model_validate({**identity, "exchange_rate": exchange})
    wire = legacy.model_dump(mode="json")
    assert list(wire) == [
        "request_id",
        "model_alias",
        "exchange_rate",
        "helper_foreground",
        "helper_background",
        "traffic_profile",
    ]
    assert native_budget_threshold_usd(Decimal("1"), Decimal("7")) == Decimal("0.142857142")
    usd = NativeRunnerLaunch.model_validate({**identity, "budget_currency": "USD"})
    assert usd.model_dump(mode="json")["budget_currency"] == "USD"
    assert "exchange_rate" not in usd.model_dump(mode="json")
    for ambiguous in (identity, {**identity, "budget_currency": "USD", "exchange_rate": exchange}):
        with pytest.raises(ValidationError):
            NativeRunnerLaunch.model_validate(ambiguous)


def test_native_topup_has_one_original_currency():
    keys = {"request_id": str(uuid4()), "pause_id": str(uuid4())}
    body = NativeBudgetIncreaseRequest.model_validate({**keys, "additional_usd": "0.004"})
    assert body.model_dump(mode="json") == {**keys, "additional_usd": "0.004"}
    for invalid in (keys, {**keys, "additional_usd": "1", "additional_cny": "1"}):
        with pytest.raises(ValidationError):
            NativeBudgetIncreaseRequest.model_validate(invalid)
