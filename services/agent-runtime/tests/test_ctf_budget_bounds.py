from decimal import Decimal
from pathlib import Path

from bbx_contracts.ctf import load_ctf_profile
from bbx_runtime.ctf.budget import ctf_call_reservation, ctf_max_output_tokens
from bbx_runtime.ctf.context import input_token_limit
from bbx_runtime.models import ctf_model_run_options


def test_small_window_leaves_input_room_and_shares_the_output_bound():
    model = load_ctf_profile(Path("profiles/ctf")).model.model_copy(
        update={"context_window": 128000}
    )
    assert ctf_max_output_tokens(model) == 64000
    assert ctf_model_run_options(model).get("max_tokens") == 64000
    assert input_token_limit(model, 128000) == 51200


def test_reservation_does_not_charge_an_impossible_input_plus_output_window():
    base = load_ctf_profile(Path("profiles/ctf")).model
    model = base.model_copy(
        update={
            "context_window": 1000000,
            "price": base.price.model_copy(update={"billing_mode": "fixed"}),
        }
    )
    assert ctf_max_output_tokens(model) == 393216
    assert ctf_call_reservation(model, context_tokens=1000000) == (
        Decimal(606784) * Decimal("2") + Decimal(393216) * Decimal("8")
    ) / Decimal(1000000)


def test_explicit_model_limits_govern_ctf_request_reservation_and_input_gate():
    base = load_ctf_profile(Path("profiles/ctf")).model
    model = base.model_copy(
        update={
            "context_window": 1_000_000,
            "max_input_tokens": 200_000,
            "max_output_tokens": 500_000,
            "price": base.price.model_copy(update={"billing_mode": "fixed"}),
        }
    )
    assert ctf_max_output_tokens(model) == 500_000
    assert ctf_model_run_options(model).get("max_tokens") == 500_000
    assert input_token_limit(model, 1_000_000) == 160_000
    assert ctf_call_reservation(model, context_tokens=1_000_000) == (
        Decimal(200_000) * Decimal("2") + Decimal(500_000) * Decimal("8")
    ) / Decimal(1_000_000)
