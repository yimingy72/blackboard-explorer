"""A lost response cannot spend the same sent model reservation twice."""

import copy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from bbx_contracts.ctf import load_ctf_profile
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptUsage

TURN = {"id": "turn", "generation": 1, "token": "fake"}
MEMBER = {"id": "member-1", "display_name": "Alice", "role": "teammate"}


class BudgetService:
    def __init__(self, failure):
        self.client = self
        self.failure = failure
        self.failed = False
        self.saved = None
        self.calls = []
        self.reads = 0
        self.billed = set()
        self.reservations = {}
        self.task = {
            "agent_profile": "ctf",
            "agent_profile_version": 1,
            "budget": {"max_minutes": 10},
            "ctf_control": {"budget_reservations": self.reservations},
        }

    async def runtime_state(self, _):
        self.reads += 1
        if self.failure == "query" and self.reads > 1:
            raise httpx.ReadError("Budget state is unavailable")
        if self.failure == "incomplete" and self.reads > 1:
            return {"task": {}}
        return {"task": copy.deepcopy(self.task)}

    async def get_profile(self, *_):
        profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
        return {"profile": profile.model_dump(mode="json")}

    async def get_agent_session(self, *_):
        if self.saved is None:
            raise RemoteError(404, "missing")
        return copy.deepcopy(self.saved)

    async def read_evidence(self, _):
        return b""

    async def runtime(self, _, operation, **body):
        self.calls.append(operation)
        if operation == "authorize_member":
            if "reservation_id" in body:
                self.reservations.setdefault(
                    body["reservation_id"], {"turn_id": "turn", "state": "admitted", "cost": "0.6"}
                )
        elif operation == "mark_reservation_sent":
            if self.failure == "unsent" and not self.failed:
                self.failed = True
                raise httpx.ReadError("Request failed before dispatch")
            self.reservations[body["reservation_id"]]["state"] = "sent"
        elif operation == "bill_usage":
            self.billed.add(body["request_id"])
            self.reservations.pop(body["reservation_id"], None)
            if self.failure == "settled" and not self.failed:
                self.failed = True
                raise httpx.ReadError("Billing committed but its response was lost")
        elif operation == "checkpoint":
            self.saved = {
                "session": copy.deepcopy(body["session"]),
                "opening_instructions": body["opening_instructions"],
                "origin": "native",
                "revision": body["expected_revision"] + 1,
            }
            return copy.deepcopy(self.saved)
        elif operation == "claim_messages":
            return []
        elif operation not in {"record_observation", "finish_turn", "authorize_review"}:
            raise AssertionError(operation)
        return {}


class LostProviderResponse(ScriptedChatClient):
    def __init__(self):
        super().__init__([ScriptStep(text="retry must not run")])
        self.entries = 0

    def _inner_get_response(self, **kwargs):
        self.entries += 1
        raise httpx.ReadError("Provider accepted the call but its response was lost")


def runner(service, client, monkeypatch):
    monkeypatch.setattr(
        "bbx_runtime.ctf.runner.ctf_call_reservation", lambda *_, **__: Decimal("0.6")
    )
    return CtfRunner(
        cast(CtfClient, service),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: client,
    )


@pytest.mark.parametrize(
    "failure,error",
    [("sent", httpx.ReadError), ("query", httpx.ReadError), ("incomplete", KeyError)],
)
async def test_sent_or_unverifiable_reservation_prevents_second_provider_attempt(
    monkeypatch, failure, error
):
    service = BudgetService(failure)
    client = LostProviderResponse()
    with pytest.raises(error):
        await runner(service, client, monkeypatch).run("task", MEMBER, TURN)
    assert client.entries == 1
    assert service.calls.count("authorize_member") == 1
    assert service.calls.count("mark_reservation_sent") == 1
    assert service.reservations == {"turn:1": {"turn_id": "turn", "state": "sent", "cost": "0.6"}}
    assert not service.billed
    assert "finish_turn" not in service.calls


async def test_confirmed_billing_after_lost_control_response_keeps_bounded_retry(monkeypatch):
    service = BudgetService("settled")
    client = ScriptedChatClient(
        [
            ScriptStep(text="first", usage=ScriptUsage(completion_tokens=1)),
            ScriptStep(text="second"),
        ]
    )
    await runner(service, client, monkeypatch).run("task", MEMBER, TURN)
    assert len(client.received_messages) == 2
    assert len(service.billed) == 2
    assert not service.reservations
    assert service.calls.count("finish_turn") == 1


async def test_confirmed_unsent_reservation_keeps_bounded_retry(monkeypatch):
    service = BudgetService("unsent")
    client = ScriptedChatClient([ScriptStep(text="done")])
    await runner(service, client, monkeypatch).run("task", MEMBER, TURN)
    assert len(client.received_messages) == 1
    assert service.calls.count("authorize_member") == 2
    assert service.calls.count("mark_reservation_sent") == 2
    assert not service.reservations
    assert service.calls.count("finish_turn") == 1


@pytest.mark.parametrize("review", [False, True])
async def test_review_and_unpriced_calls_keep_existing_retry_policy(monkeypatch, review):
    class LostOnce(ScriptedChatClient):
        entries = 0

        def _inner_get_response(self, **kwargs):
            self.entries += 1
            if self.entries == 1:
                raise httpx.ReadError("Simulated response loss")
            return super()._inner_get_response(**kwargs)

    service = BudgetService("query")
    client = LostOnce([ScriptStep(text="done")])
    instance = runner(service, client, monkeypatch)
    monkeypatch.setattr("bbx_runtime.ctf.runner.ctf_call_reservation", lambda *_, **__: None)
    turn = {**TURN, **({"purpose": "review"} if review else {})}
    await instance.run("task", MEMBER, turn)
    assert client.entries == 2
    assert service.reads == 1
    assert not service.reservations
    assert service.calls.count("finish_turn") == 1
