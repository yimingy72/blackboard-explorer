"""Offline regression tests for close checkpoint failures and write-only retries."""

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from agent_framework import AgentSession
from bbx_contracts.ctf import load_ctf_profile
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.ctf.session import CtfCheckpoint, load_checkpoint
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_runtime import TURN, MemoryCtf


@pytest.mark.parametrize("status", [400, 403, 409, 422])
async def test_rejected_checkpoint_does_not_mark_durable_unresolved(status):
    original = RemoteError(status, "synthetic rejection")

    class Store(MemoryCtf):
        async def runtime(self, task_id, operation, **payload):
            if operation == "checkpoint":
                raise original
            return await super().runtime(task_id, operation, **payload)

    service = Store()
    cp = cast(
        CtfCheckpoint,
        await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role"),
    )
    with pytest.raises(RemoteError) as raised:
        await cp.save()
    assert raised.value is original
    assert not cp.failed
    assert "checkpoint_failed" not in service.operations


async def test_uncertain_write_preserves_original_when_reconciliation_and_marker_fail(caplog):
    original = httpx.ReadError("synthetic transport failure")

    class Store(MemoryCtf):
        write_attempted = False

        async def runtime(self, task_id, operation, **payload):
            if operation == "checkpoint":
                self.write_attempted = True
                raise original
            if operation == "checkpoint_failed":
                self.operations.append(operation)
                raise RemoteError(409, "synthetic marker rejection")
            return await super().runtime(task_id, operation, **payload)

        async def get_agent_session(self, *args):
            if not self.write_attempted:
                return await super().get_agent_session(*args)
            raise RemoteError(503, "synthetic read failure")

    service = Store()
    cp = cast(
        CtfCheckpoint,
        await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role"),
    )
    with pytest.raises(httpx.ReadError) as raised:
        await cp.save()
    assert raised.value is original
    assert cp.failed
    assert service.operations == ["checkpoint_failed"]
    assert "turn_id=turn-1" in caplog.text
    assert "synthetic transport failure" not in caplog.text


@pytest.mark.parametrize("failure", [httpx.ReadError("synthetic"), RemoteError(503, "synthetic")])
async def test_checkpoint_retries_same_cas_payload_without_advancing_revision(failure):
    class Store(MemoryCtf):
        payloads = []

        async def runtime(self, task_id, operation, **payload):
            if operation == "checkpoint":
                self.payloads.append(copy.deepcopy(payload))
                if len(self.payloads) < 3:
                    raise failure
            return await super().runtime(task_id, operation, **payload)

    service = Store()
    cp = cast(
        CtfCheckpoint,
        await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role"),
    )
    await cp.save()
    assert cp.revision == 1
    assert len(service.payloads) == 3
    assert service.payloads[0] == service.payloads[1] == service.payloads[2]
    assert not cp.failed
    assert "checkpoint_failed" not in service.operations


@pytest.mark.parametrize("terminal", ["finished", "stale_generation", "unknown_conflict"])
async def test_pending_checkpoint_discard_requires_authoritative_terminal_state(terminal, caplog):
    class Service:
        async def runtime_state(self, task_id):
            return {
                "turns": [{**TURN, "status": "finished" if terminal == "finished" else "running"}],
                "members": [
                    {
                        "id": "member-1",
                        "generation": 2 if terminal == "stale_generation" else 1,
                        "current_turn_id": TURN["id"],
                    }
                ],
            }

    class Checkpoint:
        session = AgentSession()
        attempts = 0

        async def save(self):
            self.attempts += 1
            raise RemoteError(409, "synthetic conflict")

    cp = Checkpoint()
    runner = CtfRunner(cast(CtfClient, Service()), cast(Any, SimpleNamespace()))
    runner.pending_checkpoints[("task", "member-1")] = (
        cp,
        {"id": "member-1"},
        TURN,
        "private answer",
    )
    if terminal == "unknown_conflict":
        with pytest.raises(RemoteError):
            await runner.retry_checkpoints("task")
        assert runner.pending_checkpoints
        assert not caplog.records
    else:
        await runner.retry_checkpoints("task")
        await runner.retry_checkpoints("task")
        assert not runner.pending_checkpoints
        assert cp.attempts == 1
        assert len(caplog.records) == 1
        assert "private answer" not in caplog.text


@pytest.mark.parametrize("durable_usage", [True, False])
async def test_terminal_pending_checkpoint_retains_unconfirmed_local_usage(durable_usage):
    class Service:
        async def runtime_state(self, _):
            return {"turns": [{**TURN, "status": "finished"}]}

        async def runtime(self, task_id, operation, **payload):
            assert operation == "reconcile_usage_outbox"
            return {"request_ids": ["usage"] if durable_usage else []}

    class Checkpoint:
        session = SimpleNamespace(state={"ctf_usage_outbox": [{"request_id": "usage"}]})

        async def save(self):
            raise RemoteError(409, "synthetic terminal turn")

    runner = CtfRunner(cast(CtfClient, Service()), cast(Any, SimpleNamespace()))
    runner.pending_checkpoints[("task", "member-1")] = (Checkpoint(), {"id": "member-1"}, TURN, "")
    if durable_usage:
        await runner.retry_checkpoints("task")
        assert not runner.pending_checkpoints
    else:
        with pytest.raises(RemoteError):
            await runner.retry_checkpoints("task")
        assert runner.pending_checkpoints


@pytest.mark.parametrize("persistent", [False, True])
async def test_sent_model_and_completed_tool_are_not_replayed_for_checkpoint_transport_retry(
    persistent,
):
    profile = load_ctf_profile(Path("profiles/ctf"))

    class Service(MemoryCtf):
        def __init__(self):
            super().__init__()
            self.client = self
            self.failed_tool_save = False
            self.settlements = []
            self.bills = []

        async def runtime_state(self, _):
            return {
                "task": {
                    "agent_profile": "ctf",
                    "agent_profile_version": 1,
                    "budget": {"max_minutes": 1},
                    "ctf_control": {
                        "budget_reservations": {"sent": {"turn_id": TURN["id"], "state": "sent"}}
                    },
                }
            }

        async def get_profile(self, *_):
            return {"profile": profile.model_dump(mode="json")}

        async def runtime(self, task_id, operation, **payload):
            if operation == "checkpoint":
                state = payload["session"].get("state", {})
                if (persistent and state.get("ctf_usage_outbox")) or (
                    state.get("bbx_tool_results") and not self.failed_tool_save
                ):
                    self.failed_tool_save = True
                    raise httpx.ReadError("synthetic completed call write failure")
            if operation == "finish_turn":
                self.settlements.append(payload)
                return {}
            if operation == "bill_usage":
                self.bills.append(payload)
                return {}
            return await super().runtime(task_id, operation, **payload)

    class Envd:
        calls = []

        async def execute(self, task_id, member_id, command):
            self.calls.append(command)
            return "synthetic tool result"

    service, envd = Service(), Envd()
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "synthetic"}),)),
            ScriptStep(text="synthetic complete", expect_contains="synthetic tool result"),
        ]
    )
    runner = CtfRunner(
        cast(CtfClient, service),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: client,
        envd=envd,
    )
    member = {"id": "member-1", "display_name": "Alice", "role": "teammate"}
    turn = {**TURN, "token": "fake"}
    if persistent:
        with pytest.raises(httpx.ReadError):
            await runner.run("task", member, turn)
        cp = runner.pending_checkpoints[("task", "member-1")][0]
        assert cp.failed
        assert len(cp.session.state["ctf_usage_outbox"]) == 1
        assert len(client.received_options) == 1
        assert not envd.calls
        assert not service.bills
        assert not service.settlements
    else:
        await runner.run("task", member, turn)
        assert service.failed_tool_save
        assert envd.calls == ["synthetic"]
        assert len(client.received_options) == 2
        assert len(service.bills) == 2
        assert service.settlements[0]["end_reason"] == "completed"


async def test_terminal_checkpoint_with_failed_marker_cannot_silently_drop_uncertain_snapshot():
    class Service:
        async def runtime_state(self, _):
            return {"task": {"ctf_control": {}}, "turns": [{**TURN, "status": "finished"}]}

    class Checkpoint:
        failed = True
        session = AgentSession()

        async def save(self):
            raise RemoteError(409, "synthetic finished turn")

    runner = CtfRunner(cast(CtfClient, Service()), cast(Any, SimpleNamespace()))
    runner.pending_checkpoints[("task", "member-1")] = (Checkpoint(), {"id": "member-1"}, TURN, "")
    with pytest.raises(RemoteError):
        await runner.retry_checkpoints("task")
    assert runner.pending_checkpoints


async def test_interrupted_settlement_logs_identity_without_remote_error_body(caplog):
    profile = load_ctf_profile(Path("profiles/ctf"))

    class Service(MemoryCtf):
        def __init__(self):
            super().__init__()
            self.client = self
            self.settlements = []

        async def get_profile(self, *_):
            return {"profile": profile.model_dump(mode="json")}

        async def runtime_state(self, _):
            return {
                "task": {
                    "agent_profile": "ctf",
                    "agent_profile_version": 1,
                    "budget": {"max_minutes": 1},
                }
            }

        async def runtime(self, task_id, operation, **payload):
            if operation == "record_observation":
                raise RemoteError(403, "private diagnostic body")
            if operation == "finish_turn":
                self.settlements.append(payload)
                return {}
            return await super().runtime(task_id, operation, **payload)

    service = Service()
    client = ScriptedChatClient([])
    runner = CtfRunner(
        cast(CtfClient, service), cast(Any, SimpleNamespace()), client_factory=lambda *_: client
    )
    await runner.run(
        "task",
        {"id": "member-1", "display_name": "Alice", "role": "teammate"},
        {**TURN, "token": "fake"},
    )
    assert service.settlements[0]["end_reason"] == "interrupted"
    assert (
        "turn_id=turn-1 generation=1 status=403 answer_length=0 checkpoint_failed=False"
        in caplog.text
    )
    assert "private diagnostic body" not in caplog.text
    assert not client.received_options
