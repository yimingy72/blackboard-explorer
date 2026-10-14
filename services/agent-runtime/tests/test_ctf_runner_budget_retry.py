"""A lost response cannot spend the same sent model reservation twice."""

import copy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import httpx2
import pytest
from bbx_contracts.billing import add_usage
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
        self.billing_requests = []
        self.finishes = []
        self.unknown = {}
        self.turn = {
            **TURN,
            "agent_id": MEMBER["id"],
            "purpose": "execution",
            "status": "running",
            "usage": {},
        }
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
        return {
            "task": copy.deepcopy(self.task),
            "members": [
                {
                    "id": MEMBER["id"],
                    "generation": 1,
                    "current_turn_id": None if self.finishes else TURN["id"],
                }
            ],
            "turns": [copy.deepcopy(self.turn)] if not self.finishes else [],
        }

    async def state(self, _):
        return {
            "task": copy.deepcopy(self.task),
            "turns": [copy.deepcopy(self.turn)],
            "members": [
                {
                    "id": MEMBER["id"],
                    "generation": 1,
                    "current_turn_id": None if self.finishes else TURN["id"],
                }
            ],
        }

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
            self.billing_requests.append(copy.deepcopy(body))
            if body["request_id"] not in self.billed:
                self.turn["usage"] = add_usage(self.turn["usage"], body["usage"])
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
        elif operation == "finish_turn":
            self.finishes.append(copy.deepcopy(body))
            for key, entry in list(self.reservations.items()):
                if entry["state"] == "sent":
                    self.unknown[key] = {**entry, "state": "unknown"}
                del self.reservations[key]
            self.task["ctf_control"]["budget_unknown_reservations"] = self.unknown
            self.turn.update(
                status="finished", end_reason=body["end_reason"], final_answer=body["answer"]
            )
            return copy.deepcopy(self.turn)
        elif operation not in {"record_observation", "finish_turn", "authorize_review"}:
            raise AssertionError(operation)
        return {}


class LostProviderResponse(ScriptedChatClient):
    def __init__(self):
        super().__init__([ScriptStep(text="retry must not run")])
        self.entries = 0

    def _inner_get_response(self, **kwargs):
        self.entries += 1
        raise httpx2.ReadError("Provider accepted the call but its response was lost")


def runner(service, client, monkeypatch):
    monkeypatch.setattr(
        "bbx_runtime.ctf.runner.ctf_call_reservation", lambda *_, **__: Decimal("0.6")
    )
    return CtfRunner(
        cast(CtfClient, service),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: client,
    )


@pytest.mark.parametrize("failure", ["sent", "query", "incomplete"])
async def test_sent_or_unverifiable_reservation_never_replays_provider(monkeypatch, failure):
    service = BudgetService(failure)
    client = LostProviderResponse()
    await runner(service, client, monkeypatch).run("task", MEMBER, TURN)
    assert client.entries == 1
    assert service.reads == 1  # No speculative budget query can justify a whole model replay.
    assert service.calls.count("authorize_member") == 1
    assert service.calls.count("mark_reservation_sent") == 1
    assert service.unknown == {"turn:1": {"turn_id": "turn", "state": "unknown", "cost": "0.6"}}
    assert not service.billed and not service.reservations
    assert service.finishes[0]["end_reason"] == "provider_transient"
    assert service.calls.count("finish_turn") == 1
    assert service.saved is not None


async def test_confirmed_billing_response_loss_reconciles_same_receipt_without_provider_replay(
    monkeypatch,
):
    service = BudgetService("settled")
    client = ScriptedChatClient(
        [
            ScriptStep(text="first", usage=ScriptUsage(completion_tokens=1)),
            ScriptStep(text="second"),
        ]
    )
    await runner(service, client, monkeypatch).run("task", MEMBER, TURN)
    assert len(client.received_messages) == 1
    assert len(service.billed) == 1
    assert len(service.billing_requests) == 2
    assert service.billing_requests[0] == service.billing_requests[1]
    assert service.turn["usage"] == add_usage({}, service.billing_requests[0]["usage"])
    assert service.finishes[0]["answer"] == "first"
    assert service.finishes[0]["end_reason"] == "interrupted"
    assert service.saved is not None
    assert service.saved["session"]["state"]["ctf_usage_outbox"] == []
    assert any(
        message["role"] == "assistant" and message["contents"][0].get("text") == "first"
        for message in service.saved["session"]["state"]["in_memory"]["messages"]
    )
    assert not service.reservations
    assert service.calls.count("finish_turn") == 1


async def test_unsent_boundary_interrupts_before_provider_and_releases_admitted_cost(monkeypatch):
    service = BudgetService("unsent")
    client = ScriptedChatClient([ScriptStep(text="done")])
    await runner(service, client, monkeypatch).run("task", MEMBER, TURN)
    assert len(client.received_messages) == 0
    assert service.calls.count("authorize_member") == 1
    assert service.calls.count("mark_reservation_sent") == 1
    assert not service.billed and not service.unknown
    assert service.finishes[0]["end_reason"] == "interrupted"
    assert not service.reservations
    assert service.calls.count("finish_turn") == 1


@pytest.mark.parametrize("review", [False, True])
async def test_review_and_unpriced_calls_do_not_replay_without_a_reservation(monkeypatch, review):
    class LostOnce(ScriptedChatClient):
        entries = 0

        def _inner_get_response(self, **kwargs):
            self.entries += 1
            if self.entries == 1:
                raise httpx2.ReadError("Simulated provider response loss")
            return super()._inner_get_response(**kwargs)

    service = BudgetService("query")
    client = LostOnce([ScriptStep(text="done")])
    instance = runner(service, client, monkeypatch)
    monkeypatch.setattr("bbx_runtime.ctf.runner.ctf_call_reservation", lambda *_, **__: None)
    turn = {**TURN, **({"purpose": "review"} if review else {})}
    service.turn["purpose"] = "review" if review else "execution"
    if review:
        with pytest.raises(httpx2.ReadError):
            await instance.run("task", MEMBER, turn)
        assert not service.finishes
    else:
        await instance.run("task", MEMBER, turn)
        assert service.finishes[0]["end_reason"] == "provider_transient"
        assert service.calls.count("finish_turn") == 1
    assert client.entries == 1
    assert service.reads == 1
    assert not service.reservations and not service.billed
    assert service.calls.count("mark_reservation_sent") == 0
