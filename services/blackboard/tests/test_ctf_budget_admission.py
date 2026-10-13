"""Budget admission distinguishes recoverable reservations from final shortfalls."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from bbx_blackboard.ctf import check_budget
from fastapi import HTTPException


def task(*, spent="0", active="0", unknown="0", maximum="1"):
    return {
        "ctf_control": {
            "phase": "running",
            "budget_reservations": {"active": {"cost": active}},
            "budget_unknown_reservations": {"unknown": {"cost": unknown}},
        },
        "active_seconds": 0,
        "active_since": None,
        "usage": {"cost": spent},
        "budget": {"max_cost": maximum, "max_minutes": 10},
    }


@pytest.mark.parametrize(
    "state,reservation,code",
    [
        (task(active="0.6"), "0.6", "ctf_budget_wait"),
        (task(active="1"), "0", "ctf_budget_wait"),
        (task(spent="0.5"), "0.6", "ctf_budget_exhausted"),
        (task(unknown="0.5"), "0.6", "ctf_budget_exhausted"),
        (task(spent="1"), "0", "ctf_budget_exhausted"),
        (task(maximum="10", active="6.803456"), "3.401728", "ctf_budget_wait"),
    ],
)
def test_explicit_admission_codes(state, reservation, code):
    with pytest.raises(HTTPException) as caught:
        check_budget(state, Decimal(reservation))
    assert caught.value.status_code == 409
    assert isinstance(caught.value.detail, dict)
    assert cast(dict[str, Any], caught.value.detail)["code"] == code


def test_exact_fit_and_released_reservations_admit_calls():
    check_budget(task(active="0.4"), Decimal("0.6"))
    state = task(spent="0.2", active="0.6")
    with pytest.raises(HTTPException):
        check_budget(state, Decimal("0.6"))
    state["ctf_control"]["budget_reservations"] = {}
    check_budget(state, Decimal("0.6"))


def test_elapsed_budget_expires_even_while_waiting_for_usage():
    state = task(active="0.6")
    state["active_since"] = datetime.now(UTC) - timedelta(minutes=11)
    with pytest.raises(HTTPException) as caught:
        check_budget(state, Decimal("0.6"))
    assert isinstance(caught.value.detail, dict)
    assert cast(dict[str, Any], caught.value.detail)["code"] == "ctf_budget_exhausted"


def test_phase_and_replacement_conflicts_are_not_budget_waits():
    state = task()
    state["ctf_control"]["phase"] = "closing"
    with pytest.raises(HTTPException) as caught:
        check_budget(state, Decimal("0.6"))
    assert caught.value.detail == "Task is not running"
