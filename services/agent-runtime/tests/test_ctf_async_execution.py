"""Async result persistence, mailbox acknowledgement and same-turn native MAF continuation."""

import copy
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from agent_framework import AgentSession
from bbx_contracts.ctf import CtfRoleRunOptions, load_ctf_profile, resolve_ctf_model
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.budget import ctf_call_reservation
from bbx_runtime.ctf.context import input_token_limit
from bbx_runtime.ctf.execution import CtfExecutionAdapter
from bbx_runtime.ctf.failures import CtfToolFailure
from bbx_runtime.ctf.middleware import TurnBoundary
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_execution import Checkpoint, Env, Service
from test_ctf_runtime import TURN, MemoryCtf

ARGS = {"command": "inspect", "cwd": None, "timeout_sec": 120, "privileged": False}


def notice_id(task_id, member_id, turn_id, generation, command_id):
    return str(
        uuid5(
            NAMESPACE_URL,
            f"ctf-command-result:{task_id}:{member_id}:{turn_id}:{generation}:{command_id}",
        )
    )


class AsyncEnv(Env):
    supported = True
    command_state = "running"

    async def ctf_status(self, **identity):
        if identity.get("command_id"):
            return {
                **identity,
                "state": self.command_state,
                **(
                    {"result": {"exit_code": 0, "stdout": "saved evidence"}}
                    if self.command_state != "running"
                    else {}
                ),
            }
        result: dict[str, Any] = await super().ctf_status(**identity)
        if self.supported:
            result["capabilities"] = ["ctf_async_commands_v1"]
        return result

    async def ctf_operation(self, operation, identity):
        if operation == "cancel-command":
            self.command_state = "cancelled"
            return await self.ctf_status(**identity)
        return await super().ctf_operation(operation, identity)


class AsyncAdapter(CtfExecutionAdapter):
    def __init__(self, service, envd):
        super().__init__(service, envd)
        self.dispatched = []
        self.lose_start = False

    async def _request_tool(self, identity, arguments, metadata, tool_name):
        self.dispatched.append((tool_name, metadata["ctf"]["command_id"]))
        if self.lose_start:
            raise httpx.ReadError("response lost")
        return json.dumps({"structuredContent": await self.envd.ctf_status(**metadata["ctf"])})


class AsyncService(Service):
    def __init__(self, checkpoint: Checkpoint):
        super().__init__()
        self.checkpoint = checkpoint
        self.notices = {}
        self.lose_notice = False

    async def runtime(self, tid, operation, **body):
        if operation != "notify_command_result":
            return await super().runtime(tid, operation, **body)
        entry = next(
            item
            for item in self.checkpoint.saved[-1]["state"]["ctf_commands"].values()
            if item["command_id"] == body["command_id"]
        )
        assert entry["state"] == "finished" and "saved evidence" in entry["result"]
        identifier = notice_id(
            tid, body["agent_id"], body["turn_id"], body["generation"], body["command_id"]
        )
        self.notices.setdefault(identifier, {"id": identifier, "recipient_id": body["agent_id"]})
        if self.lose_notice:
            self.lose_notice = False
            raise httpx.ReadError("committed notice response lost")
        return self.notices[identifier]


async def start(adapter, checkpoint):
    return json.loads(
        await adapter.start_call(
            "task",
            "member-1",
            TURN,
            checkpoint,
            SimpleNamespace(metadata={"function_call_occurrence_id": "start"}),
            ARGS,
        )
    )


async def test_old_image_rejects_before_command_preparation_or_dispatch():
    checkpoint, envd = Checkpoint(), AsyncEnv()
    envd.supported = False
    adapter = AsyncAdapter(cast(Any, AsyncService(checkpoint)), cast(Any, envd))
    assert (await start(adapter, checkpoint))["error"]["code"] == "async_commands_unavailable"
    assert not adapter.dispatched and not checkpoint.saved
    assert "ctf_commands" not in checkpoint.session.state


async def test_pending_result_checkpoint_precedes_idempotent_notice_and_ack_checkpoint():
    checkpoint, envd = Checkpoint(), AsyncEnv()
    service = AsyncService(checkpoint)
    adapter = AsyncAdapter(cast(Any, service), cast(Any, envd))
    first = await start(adapter, checkpoint)
    assert first["state"] == "running" and len(adapter.dispatched) == 1
    busy = await adapter.execute_call(
        "task",
        "member-1",
        TURN,
        cast(Any, checkpoint),
        cast(Any, SimpleNamespace(metadata={})),
        ARGS,
    )
    assert json.loads(busy)["state"] == "busy" and len(adapter.dispatched) == 1
    envd.command_state = "completed"
    service.lose_notice = True
    await adapter.reconcile_jobs("task", "member-1", TURN, cast(Any, checkpoint))
    assert len(service.notices) == 1
    entry = adapter.job_entry(cast(Any, checkpoint), first["command_id"])
    assert entry["state"] == "finished" and entry["completion_notified"]
    assert any(
        e["state"] == "finished" and not e["completion_notified"]
        for snapshot in checkpoint.saved
        for e in snapshot["state"]["ctf_commands"].values()
    )
    await adapter.reconcile_jobs("task", "member-1", TURN, cast(Any, checkpoint))
    assert len(service.notices) == len(adapter.dispatched) == 1


async def test_lost_start_only_reads_original_command_id_and_unknown_never_replays():
    checkpoint, envd = Checkpoint(), AsyncEnv()
    adapter = AsyncAdapter(cast(Any, AsyncService(checkpoint)), cast(Any, envd))
    adapter.lose_start = True
    assert (await start(adapter, checkpoint))["state"] == "running"
    assert len(adapter.dispatched) == 1
    assert (await start(adapter, checkpoint))["state"] == "running"
    assert len(adapter.dispatched) == 1

    checkpoint2, envd2 = Checkpoint(), AsyncEnv()
    envd2.command_state = "not_found"
    failed = AsyncAdapter(cast(Any, AsyncService(checkpoint2)), cast(Any, envd2))
    failed.lose_start = True
    with pytest.raises(CtfToolFailure):
        await start(failed, checkpoint2)
    assert list(checkpoint2.session.state["ctf_commands"].values())[0]["state"] == "unknown"
    assert len(failed.dispatched) == 1


async def test_initial_completion_has_no_duplicate_notice_and_cancel_is_bounded():
    checkpoint, envd = Checkpoint(), AsyncEnv()
    envd.command_state = "completed"
    service = AsyncService(checkpoint)
    adapter = AsyncAdapter(cast(Any, service), cast(Any, envd))
    first = await start(adapter, checkpoint)
    assert first["state"] == "completed"
    await adapter.reconcile_jobs("task", "member-1", TURN, cast(Any, checkpoint))
    assert not service.notices

    checkpoint2, envd2 = Checkpoint(), AsyncEnv()
    adapter2 = AsyncAdapter(cast(Any, AsyncService(checkpoint2)), cast(Any, envd2))
    pending = await start(adapter2, checkpoint2)
    result = json.loads(
        await adapter2.cancel_call(
            "task", "member-1", TURN, cast(Any, checkpoint2), pending["command_id"]
        )
    )
    assert result == {"command_id": pending["command_id"], "state": "cancelled"}


async def test_unconfirmed_result_checkpoint_cannot_notify_and_restart_can_read_same_id():
    class FailingCheckpoint(Checkpoint):
        fail_result = False

        async def save(self):
            if self.fail_result and any(
                entry.get("state") == "finished"
                for entry in self.session.state.get("ctf_commands", {}).values()
            ):
                raise httpx.ReadError("checkpoint not confirmed")
            await super().save()

    checkpoint, envd = FailingCheckpoint(), AsyncEnv()
    service = AsyncService(checkpoint)
    adapter = AsyncAdapter(cast(Any, service), cast(Any, envd))
    await start(adapter, checkpoint)
    envd.command_state = "completed"
    checkpoint.fail_result = True
    with pytest.raises(httpx.ReadError):
        await adapter.reconcile_jobs("task", "member-1", TURN, cast(Any, checkpoint))
    assert not service.notices
    restored = Checkpoint()
    restored.session = AgentSession.from_dict(checkpoint.saved[-1])
    service.checkpoint = restored
    await adapter.reconcile_jobs("task", "member-1", TURN, cast(Any, restored))
    assert len(service.notices) == len(adapter.dispatched) == 1


async def test_lost_notice_ack_is_read_back_in_later_turn_without_reexecuting():
    class LostAckService(AsyncService):
        lose_all = True

        async def runtime(self, tid, operation, **body):
            result = await super().runtime(tid, operation, **body)
            if operation == "notify_command_result" and self.lose_all:
                raise httpx.ReadError("notice ACK lost")
            return result

    checkpoint, envd = Checkpoint(), AsyncEnv()
    service = LostAckService(checkpoint)
    adapter = AsyncAdapter(cast(Any, service), cast(Any, envd))
    await start(adapter, checkpoint)
    envd.command_state = "completed"
    with pytest.raises(httpx.ReadError):
        await adapter.reconcile_jobs("task", "member-1", TURN, cast(Any, checkpoint))
    restored = Checkpoint()
    restored.session = AgentSession.from_dict(checkpoint.saved[-1])
    restored.saved = [checkpoint.saved[-1]]
    service.checkpoint, service.lose_all = restored, False
    later = {"id": "later-turn", "generation": 2}
    assert adapter.pending_jobs(cast(Any, restored), later) == []
    await adapter.reconcile_jobs("task", "member-1", later, cast(Any, restored))
    assert len(service.notices) == len(adapter.dispatched) == 1
    assert all(
        entry["completion_notified"] for entry in restored.session.state["ctf_commands"].values()
    )


async def test_native_final_waits_same_turn_and_role_settings_are_shared_across_runs():
    profile = load_ctf_profile(Path("profiles/ctf"))
    profile.worker_tools["teammate"].builtin += [
        "start_command",
        "cancel_command",
        "read_command_result",
    ]
    profile.model = profile.model.model_copy(
        update={"context_window": 100000, "max_input_tokens": 100000, "max_output_tokens": 65536}
    )
    profile.role_run_options = {"teammate": CtfRoleRunOptions(max_output_tokens=4096)}
    original = profile.model_dump(mode="json")
    effective = resolve_ctf_model(profile, "teammate")

    class RuntimeService(MemoryCtf):
        def __init__(self):
            super().__init__()
            self.client = self
            self.reservations = []
            self.finished = []
            self.member = {
                "id": "member-1",
                "role": "teammate",
                "display_name": "Worker",
                "generation": 1,
                "current_turn_id": TURN["id"],
                "execution": {"boot_id": "boot-1", "generation": 1, "drained": False},
            }

        async def get_profile(self, *_):
            return {"profile": copy.deepcopy(original)}

        async def runtime_state(self, _):
            return {
                "task": {
                    "agent_profile": "ctf",
                    "agent_profile_version": 1,
                    "budget": {"max_minutes": 2},
                    "ctf_control": {"phase": "running"},
                },
                "members": [self.member],
                "ready_member_ids": ["member-1"]
                if any(row["id"] not in self.delivered for row in self.rows)
                else [],
            }

        async def runtime(self, task_id, operation, **body):
            if operation == "authorize_member" and body.get("reservation_id"):
                self.reservations.append(body)
            if operation == "notify_command_result":
                identifier = notice_id(
                    task_id,
                    body["agent_id"],
                    body["turn_id"],
                    body["generation"],
                    body["command_id"],
                )
                if not any(row["id"] == identifier for row in self.rows):
                    self.post(
                        identifier, "Command completed; read_command_result " + body["command_id"]
                    )
                    self.rows[-1].update(
                        sender_kind="system",
                        sender_id="system",
                        kind="notification",
                        notification_purpose="command_result",
                    )
                return {"id": identifier, "recipient_id": "member-1"}
            if operation == "finish_turn":
                self.finished.append(body)
                return body
            if operation == "bill_usage":
                return {}
            return await super().runtime(task_id, operation, **body)

    class RunnerEnv(AsyncEnv):
        reads = 0

        async def ctf_status(self, **identity):
            if identity.get("command_id"):
                self.reads += 1
                if self.reads >= 3:
                    self.command_state = "completed"
            return await super().ctf_status(**identity)

    class RunnerAdapter(AsyncAdapter):
        stopped = 0

        async def prepare(self, task_id, member, turn):
            return {}

        async def stop_member(self, task_id, member):
            self.stopped += 1
            assert service.finished == [] and len(client.received_messages) == 3
            member["execution"]["drained"] = True
            return {}

    service, envd = RuntimeService(), RunnerEnv()
    adapter = RunnerAdapter(cast(Any, service), cast(Any, envd))
    service.post("assignment", "Inspect asynchronously")
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("start_command", {"command": "inspect"}),)),
            ScriptStep(text="Waiting for the command", expect_contains='"state": "running"'),
            ScriptStep(text="Completed after notification", expect_contains="Command completed"),
        ]
    )
    factories = []

    def factory(member, resolved):
        factories.append(resolved.model_dump(mode="json"))
        type(resolved).model_validate(resolved.model_dump(mode="json"))
        return client

    runner = CtfRunner(
        cast(Any, service), cast(Any, SimpleNamespace()), client_factory=factory, envd=adapter
    )
    await runner.run("task", service.member, {**TURN, "token": "token"})
    assert adapter.stopped == len(service.finished) == len(factories) == 1
    assert service.finished[0]["answer"] == "Completed after notification"
    assert len(client.received_messages) == 3
    assert all(options["max_tokens"] == 4096 for options in client.received_options)
    assert all(
        options["reasoning_effort"] == effective.reasoning_effort
        for options in client.received_options
    )
    assert all(
        Decimal(item["reservation_cost"])
        == ctf_call_reservation(effective, context_tokens=profile.options.context_threshold)
        for item in service.reservations
    )
    assert service.saved is not None
    assert service.saved["session"]["state"]["ctf_steps"][TURN["id"]] == 3
    assert service.saved["session"]["state"]["ctf_model_view"][
        "input_limit_tokens"
    ] == input_token_limit(effective, profile.options.context_threshold)
    assert profile.model_dump(mode="json") == original
    assert runner.command_checkpoints == {}
    effective_traces = [
        observation["body"]["effective_run_options"]
        for observation in service.observations
        if "effective_run_options" in observation["body"]
    ]
    assert effective_traces == [
        {
            "reasoning_effort": effective.reasoning_effort,
            "max_output_tokens": 4096,
            "context_threshold": profile.options.context_threshold,
        }
    ]


async def test_pending_wait_respects_original_deadline_without_poll_or_model_call():
    runner = CtfRunner(cast(Any, SimpleNamespace()), cast(Any, SimpleNamespace()))
    with pytest.raises(TurnBoundary, match="budget_exhausted"):
        await runner.wait_commands("task", "member", TURN, None, None, 0)


@pytest.mark.parametrize(
    "tool_name,command_timeout,read_timeout",
    [
        ("execute_command", 1200, 1230),
        ("execute_command", 120, 150),
        ("start_command", 1200, 30),
    ],
)
async def test_transport_wait_covers_sync_execution_but_not_async_lifetime(
    monkeypatch,
    tool_name,
    command_timeout,
    read_timeout,
):
    captured = []

    class ClientCreated(Exception):
        pass

    def client(**kwargs):
        captured.append(kwargs)
        raise ClientCreated

    envd = SimpleNamespace(headers={"Authorization": "test"})
    adapter = CtfExecutionAdapter(cast(Any, SimpleNamespace()), cast(Any, envd))
    monkeypatch.setattr("bbx_runtime.ctf.execution.httpx.AsyncClient", client)
    with pytest.raises(ClientCreated):
        await adapter._request_tool(
            {"agent_id": "agent-2"}, {**ARGS, "timeout_sec": command_timeout}, {}, tool_name
        )
    assert captured[0]["timeout"].read == read_timeout
    assert captured[0]["timeout"].connect == 10
    assert captured[0]["timeout"].write == captured[0]["timeout"].pool == 30


async def test_temporary_budget_reservations_do_not_drain_an_admitted_command(monkeypatch):
    calls = []

    async def runtime(task_id, operation, **body):
        calls.append(operation)
        assert operation == "authorize_member"
        raise RemoteError(409, "Another model call holds the reservation", "ctf_budget_wait")

    async def runtime_state(task_id):
        return {"ready_member_ids": []}

    async def sleep(_):
        pass

    class Pending:
        polls = 0

        async def reconcile_jobs(self, *args):
            self.polls += 1

        def pending_jobs(self, *args):
            return ["pending"] if self.polls < 2 else []

    adapter = Pending()
    monkeypatch.setattr("bbx_runtime.ctf.runner.asyncio.sleep", sleep)
    runner = CtfRunner(
        cast(Any, SimpleNamespace(runtime=runtime, runtime_state=runtime_state)),
        cast(Any, SimpleNamespace()),
    )
    await runner.wait_commands("task", "member", TURN, None, adapter, 1e20)
    assert adapter.polls == 2 and calls == ["authorize_member", "authorize_member"]
