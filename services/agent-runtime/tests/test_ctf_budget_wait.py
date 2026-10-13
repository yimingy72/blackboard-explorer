"""Offline admission waits preserve a turn until usage settles or it is stopped."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.coordinator import budget_exhausted
from bbx_runtime.ctf.middleware import MailboxMiddleware, TurnBoundary


class AdmissionService:
    def __init__(self, code="ctf_budget_wait"):
        self.code = code
        self.available = False
        self.attempted = asyncio.Event()
        self.calls = []

    async def runtime(self, task_id, operation, **body):
        self.calls.append((operation, body))
        if operation == "authorize_member" and not self.available:
            self.attempted.set()
            raise RemoteError(409, "admission", self.code)
        return {}


def mailbox(service):
    checkpoint = SimpleNamespace(
        task_id="task", agent_id="member", session=SimpleNamespace(state={})
    )
    return MailboxMiddleware(
        cast(Any, service),
        cast(Any, checkpoint),
        {"id": "turn", "generation": 1},
        10,
        Decimal("0.6"),
    )


async def test_wait_resumes_same_turn_without_consuming_steps_or_claiming_inputs():
    service = AdmissionService()
    middleware = mailbox(service)
    reconciled, model_calls = [], []

    async def reconcile():
        reconciled.append(True)

    async def model():
        await middleware.enter_provider()
        model_calls.append(True)

    middleware.reconcile = reconcile
    pending = asyncio.create_task(middleware.process(cast(Any, None), model))
    await asyncio.wait_for(service.attempted.wait(), 1)
    assert middleware.steps == 0
    assert not reconciled and not model_calls
    assert [operation for operation, _ in service.calls] == ["authorize_member"]
    service.available = True
    await asyncio.wait_for(pending, 1)
    assert middleware.steps == 1 and reconciled == [True] and model_calls == [True]
    authorizations = [body for operation, body in service.calls if operation == "authorize_member"]
    assert len(authorizations) == 2 and authorizations[0] == authorizations[1]
    assert authorizations[0]["reservation_id"] == "turn:1"
    assert [operation for operation, _ in service.calls][-1] == "mark_reservation_sent"


async def test_wait_is_cancellable_without_model_call_or_turn_settlement():
    service = AdmissionService()
    middleware = mailbox(service)

    async def model():
        pytest.fail("A waiting call must not enter the provider")

    pending = asyncio.create_task(middleware.process(cast(Any, None), model))
    await asyncio.wait_for(service.attempted.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert middleware.steps == 0
    assert [operation for operation, _ in service.calls] == ["authorize_member"]


@pytest.mark.parametrize("code", [None, "stale_generation"])
async def test_unrelated_conflicts_are_not_retried(code):
    service = AdmissionService(code)
    middleware = mailbox(service)
    with pytest.raises(RemoteError):
        await middleware.process(cast(Any, None), cast(Any, None))
    assert len(service.calls) == 1 and middleware.steps == 0


async def test_permanent_shortfall_closes_budget_before_any_model_step():
    service = AdmissionService("ctf_budget_exhausted")
    middleware = mailbox(service)
    with pytest.raises(TurnBoundary, match="budget_exhausted"):
        await middleware.process(cast(Any, None), cast(Any, None))
    assert middleware.steps == 0
    assert [operation for operation, _ in service.calls] == ["authorize_member", "request_finish"]
    assert service.calls[-1][1]["conclusion"]["end_reason"] == "budget_exhausted"


def test_active_reservations_are_not_final_exhaustion_but_unknown_costs_are():
    task = {
        "ctf_control": {"budget_reservations": {"active": {"cost": "0.8"}}},
        "usage": {"cost": "0.2"},
        "budget": {"max_cost": "1", "max_minutes": 10},
    }
    assert not budget_exhausted(task)
    task["ctf_control"]["budget_unknown_reservations"] = {"unknown": {"cost": "0.8"}}
    assert budget_exhausted(task)


@pytest.mark.parametrize("nested", [False, True])
async def test_http_boundary_preserves_explicit_admission_code(nested):
    detail = {"code": "ctf_budget_wait", "message": "Reserved by active calls"}
    response = httpx.Response(409, json={"detail": detail} if nested else detail)
    with pytest.raises(RemoteError) as caught:
        await BlackboardClient._check(response)
    assert caught.value.code == "ctf_budget_wait"
    assert caught.value.message == "Reserved by active calls"
