"""One member's control drop does not cancel peers or erase an uncommitted failure."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from bbx_contracts.ctf import CtfFailureDiagnostic
from bbx_runtime.ctf.client import CtfClient, CtfControlTransportExhausted
from bbx_runtime.ctf.coordinator import CtfCoordinator, failure_diagnostic


class Store:
    def __init__(self):
        self.phase = "running"
        self.fail_close_once = False
        self.conclusion = None
        self.members = [
            {"id": identity, "lifecycle": "active"} for identity in ("lead", "member-1", "member-2")
        ]

    async def runtime_state(self, _):
        return deepcopy(
            {
                "task": {
                    "status": "finished" if self.phase == "closed" else "running",
                    "ctf_control": {"phase": self.phase},
                    "budget": {"max_cost": "100", "max_minutes": 60},
                    "usage": {"cost": "0"},
                },
                "members": self.members,
                "turns": [],
                "ready_member_ids": [],
                "review_ready_member_ids": [],
            }
        )

    async def runtime(self, _, operation, **body):
        if operation == "request_finish":
            if self.fail_close_once:
                self.fail_close_once = False
                raise httpx.RemoteProtocolError("synthetic closing connection lost")
            self.phase = "closing"
            self.conclusion = body["conclusion"]
        return {}


class Runner:
    def __init__(self):
        self.pending_checkpoints = {}
        self.pending_settlements = {}
        self.settled = []
        self.checkpoints_retried = 0
        self.settlements_retried = 0

    async def retry_checkpoints(self, _):
        self.checkpoints_retried += 1
        self.pending_checkpoints.clear()

    async def retry_settlements(self, _):
        self.settlements_retried += 1

    async def settle(self, task, member, turn, reason, answer):
        self.settled.append((task, member["id"], turn["id"], reason, answer))
        return True


def coordinator(store, runner):
    value = CtfCoordinator(cast(Any, None), cast(Any, SimpleNamespace()), runner=runner)
    value.service = cast(CtfClient, store)
    value.watchdog_checks["task"] = float("inf")
    return value


async def failed(error):
    async def run():
        raise error

    future = asyncio.create_task(run())
    await asyncio.sleep(0)
    return future


async def test_turn_transport_drop_settles_only_affected_member_and_preserves_peer(caplog):
    store, runner = Store(), Runner()
    value = coordinator(store, runner)
    bad = await failed(httpx.RemoteProtocolError("synthetic private request"))
    peer = asyncio.create_task(asyncio.Event().wait())
    turn = {"id": "turn-1", "generation": 2}
    value.active[("task", "member-1")] = (bad, turn)
    value.active[("task", "member-2")] = (peer, {"id": "turn-2", "generation": 1})
    try:
        assert await value.tick("task")
        assert store.phase == "running" and store.conclusion is None
        assert not peer.cancelled() and not peer.done()
        assert runner.settled[0][:4] == ("task", "member-1", "turn-1", "interrupted")
        assert "synthetic private request" not in caplog.text
    finally:
        peer.cancel()
        await asyncio.gather(peer, return_exceptions=True)


async def test_pending_checkpoint_is_retried_after_failed_future_without_second_settlement():
    store, runner = Store(), Runner()
    value = coordinator(store, runner)
    key = ("task", "member-1")
    runner.pending_checkpoints[key] = object()
    value.active[key] = (
        await failed(httpx.RemoteProtocolError("synthetic checkpoint drop")),
        {"id": "turn-1", "generation": 1},
    )
    assert await value.tick("task")
    assert runner.checkpoints_retried == 1
    assert runner.settled == [] and store.phase == "running"


async def test_uncommitted_failure_close_keeps_future_until_authoritative_close():
    store, runner = Store(), Runner()
    store.fail_close_once = True
    value = coordinator(store, runner)
    key = ("task", "lead")
    value.active[key] = (
        await failed(ValueError("synthetic deterministic failure")),
        {"id": "t", "generation": 1},
    )
    with pytest.raises(httpx.RemoteProtocolError):
        await value.tick("task")
    assert key in value.active and store.phase == "running"
    assert await value.tick("task")
    assert key not in value.active and store.phase == "closing"
    assert store.conclusion is not None
    assert store.conclusion["failure"]["member_id"] == "lead"


async def test_bounded_settlement_exhaustion_keeps_identity_in_failure():
    store, runner = Store(), Runner()
    value = coordinator(store, runner)
    error = CtfControlTransportExhausted("task", "member-1", "turn-1", 3, "confirm_finish")
    value.active[("task", "member-1")] = (await failed(error), {"id": "turn-1", "generation": 3})
    assert await value.tick("task")
    assert store.conclusion is not None
    failure = store.conclusion["failure"]
    assert failure["phase"] == "confirm_finish"
    assert failure["task_id"] == "task" and failure["turn_id"] == "turn-1"
    assert failure["member_id"] == "member-1" and failure["generation"] == 3
    assert CtfFailureDiagnostic.model_validate(failure)


async def test_review_retry_preserves_original_pending_result():
    store, runner = Store(), Runner()
    value = coordinator(store, runner)
    value.review_recovered.add("task")
    key = ("task", "member-1")
    turn = {"id": "review-1", "generation": 4, "purpose": "review"}
    original = ({"id": "member-1"}, turn, "completed", "Original confirmed reply")
    runner.pending_settlements[key] = original
    value.review_active[key] = (
        await failed(httpx.RemoteProtocolError("synthetic review drop")),
        turn,
    )
    await value.review_tick("task")
    assert runner.pending_settlements[key] == original
    assert runner.settled == [("task", "member-1", "review-1", "completed", original[3])]


def test_failure_log_and_contract_include_only_safe_context(caplog):
    diagnostic = failure_diagnostic(
        "turn",
        RuntimeError("synthetic secret body"),
        task_id="task",
        member_id="member-1",
        turn_id="turn",
        generation=2,
    )
    assert CtfFailureDiagnostic.model_validate(diagnostic)
    assert "task_id=task member_id=member-1 turn_id=turn generation=2" in caplog.text
    assert "synthetic secret body" not in caplog.text


async def test_startup_read_drop_is_reconciled_without_fail_start(monkeypatch):
    store, runner = Store(), Runner()
    store.phase = "provisioning"
    value = coordinator(store, runner)
    state = store.runtime_state
    calls = 0

    async def flaky(task_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("synthetic startup read drop")
        return await state(task_id)

    starts = []

    async def start(_, operation, **body):
        starts.append(operation)
        if operation == "start":
            store.phase = "running"
        return {}

    store.runtime_state, store.runtime = cast(Any, flaky), start
    monkeypatch.setattr("bbx_runtime.ctf.coordinator.asyncio.sleep", AsyncMock())
    value.tick = AsyncMock(return_value=False)
    await value.run("task")
    assert starts == ["start"]


async def test_committed_start_response_loss_is_read_back_without_resending(monkeypatch):
    store, runner = Store(), Runner()
    store.phase = "provisioning"
    value = coordinator(store, runner)
    operations = []

    async def start(_, operation, **body):
        operations.append(operation)
        store.phase = "running"
        raise httpx.RemoteProtocolError("synthetic committed startup response loss")

    store.runtime = start
    monkeypatch.setattr("bbx_runtime.ctf.coordinator.asyncio.sleep", AsyncMock())
    value.tick = AsyncMock(return_value=False)
    await value.run("task")
    assert operations == ["start"] and store.phase == "running"


async def test_unknown_start_is_bounded_and_never_dispatched_twice(monkeypatch):
    store, runner = Store(), Runner()
    store.phase = "provisioning"
    value = coordinator(store, runner)
    operations = []

    async def start(_, operation, **body):
        operations.append(operation)
        if operation == "start":
            raise httpx.RemoteProtocolError("synthetic unconfirmed start")
        return {}

    store.runtime = start
    pause = AsyncMock()
    monkeypatch.setattr("bbx_runtime.ctf.coordinator.asyncio.sleep", pause)
    with pytest.raises(httpx.ReadError):
        await value.run("task")
    assert operations == ["start", "fail_start"]
    assert pause.await_count == 2


async def test_joined_review_exhaustion_recovers_only_its_turn_and_preserves_answer():
    store, runner = Store(), Runner()
    store.phase = "closed"
    value = coordinator(store, runner)
    value.review_recovered.add("task")
    key = ("task", "member-1")
    turn = {"id": "review-1", "generation": 4, "purpose": "review"}
    answer = "Original saved review answer"
    runner.pending_settlements[key] = ({"id": key[1]}, turn, "completed", answer)
    runner.settle = AsyncMock(
        side_effect=CtfControlTransportExhausted("task", key[1], turn["id"], 4, "confirm_finish")
    )
    value.review_active[key] = (
        await failed(httpx.RemoteProtocolError("synthetic review drop")),
        turn,
    )
    calls = []

    async def recover(task, operation, **body):
        calls.append((operation, body))
        return {
            "recovered": 1,
            "turn": {
                **turn,
                "status": "finished",
                "agent_id": key[1],
                "end_reason": "interrupted",
                "final_answer": answer,
            },
        }

    store.runtime = cast(Any, recover)
    await value.review_tick("task")
    assert len(calls) == 1 and calls[0][0] == "recover_reviews"
    assert calls[0][1]["failed_turn_id"] == turn["id"]
    assert calls[0][1]["runtime_instance"] == value.instance and calls[0][1]["joined"] is True
    assert key not in runner.pending_settlements and key not in value.review_active


async def test_joined_review_recovery_rejection_is_throttled_and_preserves_pending(monkeypatch):
    from bbx_runtime.clients.blackboard import RemoteError

    store, runner = Store(), Runner()
    store.phase = "closed"
    value = coordinator(store, runner)
    value.review_recovered.add("task")
    key = ("task", "member-1")
    turn = {"id": "review-1", "generation": 4, "purpose": "review"}
    original = ({"id": key[1]}, turn, "completed", "Saved answer")
    runner.pending_settlements[key] = original
    runner.settle = AsyncMock(
        side_effect=CtfControlTransportExhausted("task", key[1], turn["id"], 4, "confirm_finish")
    )
    store.runtime = AsyncMock(side_effect=RemoteError(409, "Synthetic unresolved checkpoint"))
    monkeypatch.setattr("bbx_runtime.ctf.coordinator.monotonic", lambda: 10.0)
    await value.review_tick("task")
    await value.review_tick("task")
    assert store.runtime.await_count == runner.settle.await_count == 1
    assert runner.pending_settlements[key] == original


async def test_joined_review_recovery_response_loss_is_idempotently_confirmed(monkeypatch):
    store, runner = Store(), Runner()
    store.phase = "closed"
    value = coordinator(store, runner)
    value.review_recovered.add("task")
    key = ("task", "member-1")
    turn = {"id": "review-1", "generation": 4, "purpose": "review"}
    original = ({"id": key[1]}, turn, "completed", "Original saved reply")
    runner.pending_settlements[key] = original
    runner.settle = AsyncMock(
        side_effect=CtfControlTransportExhausted("task", key[1], turn["id"], 4, "confirm_finish")
    )
    saved = {
        **turn,
        "agent_id": key[1],
        "status": "finished",
        "end_reason": "interrupted",
        "final_answer": original[3],
    }
    calls = 0

    async def recover(_, operation, **body):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("Synthetic committed recovery response loss")
        return {"recovered": 0, "turn": saved}

    store.runtime = cast(Any, recover)
    clock = [10.0]
    monkeypatch.setattr("bbx_runtime.ctf.coordinator.monotonic", lambda: clock[0])
    await value.review_tick("task")
    assert runner.pending_settlements[key] == original
    clock[0] = 41.0
    await value.review_tick("task")
    assert key not in runner.pending_settlements and calls == 2


async def test_confirmed_member_stop_discards_only_its_superseded_settlement():
    store, runner = Store(), Runner()
    value = coordinator(store, runner)
    key = ("task", "member-1")
    member = {
        "id": key[1],
        "generation": 4,
        "current_turn_id": "turn-1",
        "pending_operation": {"kind": "stop", "request_id": "stop-1"},
    }
    turn = {"id": "turn-1", "generation": 4, "purpose": "execution"}
    runner.pending_settlements[key] = (member, turn, "completed", "Saved original answer")
    peer = ("task", "member-2")
    runner.pending_settlements[peer] = (
        store.members[2],
        {"id": "peer", "generation": 2},
        "completed",
        "Peer reply",
    )
    store.members = [member]
    store.runtime = AsyncMock(
        return_value={"id": key[1], "operation_history": [{"request_id": "stop-1"}]}
    )
    value.envd = SimpleNamespace(stop_member=AsyncMock(return_value={"drained": True}))
    await value.member_operations("task", await store.runtime_state("task"))
    assert key not in runner.pending_settlements and peer in runner.pending_settlements


@pytest.mark.parametrize("review", [False, True])
async def test_pending_settlement_blocks_new_dispatch_only_for_affected_member(review):
    store, runner = Store(), Runner()
    value = coordinator(store, runner)
    key = ("task", "member-1")
    purpose = "review" if review else "execution"
    turn = {"id": "old-turn", "generation": 4, "purpose": purpose}
    runner.pending_settlements[key] = ({"id": key[1]}, turn, "completed", "Original reply")
    runner.settle = AsyncMock(return_value=False)
    state = await store.runtime_state("task")
    if review:
        value.review_recovered.add("task")
        state["task"]["status"] = "finished"
        state["review_ready_member_ids"] = [key[1]]
    else:
        state["ready_member_ids"] = [key[1]]
    store.runtime_state = AsyncMock(return_value=state)
    store.runtime = AsyncMock(return_value=None)
    if review:
        await value.review_tick("task")
    else:
        await value.tick("task")
    assert not any(
        call.args[1] in {"claim_turn", "claim_review_turn"} for call in store.runtime.call_args_list
    )
    assert runner.pending_settlements[key][1] == turn
