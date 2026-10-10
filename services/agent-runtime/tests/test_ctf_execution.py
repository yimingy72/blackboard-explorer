"""CTF execution identity and command persistence without shell or network access."""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agent_framework import AgentSession
from bbx_runtime.ctf.execution import CtfExecutionAdapter, UnknownCommandOutcome


class Env:
    boot = "boot-1"
    state = "ready"

    async def ctf_status(self):
        return {"task_id": "task", "boot_id": self.boot, "state": self.state}

    async def create_user(self, aid):
        return {"agent_id": aid}

    async def ctf_operation(self, operation, identity):
        return {
            **identity,
            "state": "drained" if operation == "drain" else "registered",
            "drained": operation == "drain",
        }


class Service:
    def __init__(self):
        self.member = {
            "id": "member-1",
            "generation": 1,
            "execution": {"boot_id": "boot-1", "generation": 1, "drained": False},
        }
        self.operations = []

    async def state(self, _):
        return {"members": [self.member]}

    async def runtime(self, tid, operation, **body):
        self.operations.append((operation, body))
        return {"uri": "traces/task/member-1/output.json"}


class Checkpoint:
    def __init__(self):
        self.session = AgentSession()
        self.saved = []

    async def save(self):
        self.saved.append(self.session.to_dict())


class Adapter(CtfExecutionAdapter):
    def __init__(self, service, envd):
        super().__init__(service, envd)
        self.calls = []

    async def _call(self, identity, arguments, metadata):
        self.calls.append((identity, arguments, metadata))
        return json.dumps({"structuredContent": {"stdout": "ok"}})


async def test_command_uuid_and_args_are_saved_before_mcp_and_retry_reuses_result():
    service, checkpoint = Service(), Checkpoint()
    adapter = Adapter(cast(Any, service), cast(Any, Env()))
    context = SimpleNamespace(metadata={"function_call_occurrence_id": "occurrence"})
    turn = {"id": "turn", "generation": 1}
    args = {"command": "inspect", "timeout_sec": 120, "cwd": None, "privileged": False}
    first = await adapter.execute_call(
        "task", "member-1", turn, cast(Any, checkpoint), cast(Any, context), args
    )
    second = await adapter.execute_call(
        "task", "member-1", turn, cast(Any, checkpoint), cast(Any, context), args
    )
    assert first == second
    assert len(adapter.calls) == 1
    identity, _, meta = adapter.calls[0]
    assert identity["agent_id"] == "agent-2"
    persisted = checkpoint.saved[0]["state"]["ctf_commands"]["turn:occurrence"]
    assert persisted["state"] == "prepared"
    assert persisted["command_id"] == meta["ctf"]["command_id"]
    with pytest.raises(UnknownCommandOutcome, match="arguments changed"):
        await adapter.execute_call(
            "task",
            "member-1",
            turn,
            cast(Any, checkpoint),
            cast(Any, context),
            {**args, "command": "different"},
        )


async def test_old_boot_and_unknown_registry_never_claim_drain_or_execute():
    service, env = Service(), Env()
    adapter = Adapter(cast(Any, service), cast(Any, env))
    env.boot = "boot-2"
    with pytest.raises(UnknownCommandOutcome, match="boot changed"):
        await adapter.stop_member("task", service.member)
    assert not service.operations
    env.state = "unknown"
    assert await adapter.drain("task") is False
    assert not adapter.calls


async def test_drain_persists_matching_proof_before_backend_stop_can_complete():
    service = Service()
    adapter = Adapter(cast(Any, service), cast(Any, Env()))
    proof = await adapter.stop_member("task", service.member)
    assert proof["drained"] and proof["generation"] == 1
    assert service.operations == [
        ("record_execution_drained", {"agent_id": "member-1", "proof": proof})
    ]


async def test_full_command_output_registered_as_fenced_log_and_foreign_path_rejected():
    class OutputEnv(Env):
        async def stat(self, path):
            return {"is_file": True, "size": 3}

        async def read_file(self, path):
            return b"all"

    service = Service()
    adapter = Adapter(cast(Any, service), cast(Any, OutputEnv()))
    turn = {"id": "turn", "generation": 1}
    result = await adapter.preserve_output(
        "task",
        "member-1",
        turn,
        "command",
        json.dumps(
            {"structuredContent": {"full_output_path": "/workspace/agents/agent-2/.outputs/1.txt"}}
        ),
    )
    assert json.loads(result)["structuredContent"]["full_output_uri"].startswith("traces/task/")
    operation, body = service.operations[0]
    assert operation == "record_observation" and body["generation"] == 1
    assert body["body"]["content_base64"] == "YWxs"
    with pytest.raises(UnknownCommandOutcome, match="does not belong"):
        await adapter.preserve_output(
            "task",
            "member-1",
            turn,
            "other",
            json.dumps(
                {
                    "structuredContent": {
                        "full_output_path": "/workspace/agents/agent-1/.outputs/1.txt"
                    }
                }
            ),
        )


async def test_destroyed_container_proof_finishes_old_stop_without_new_boot_registration():
    service, env = Service(), Env()
    service.member["execution"].update(
        container_destroyed=True, drained=True, replacement_boot_id="boot-2"
    )
    env.boot = "boot-2"
    adapter = Adapter(cast(Any, service), cast(Any, env))
    proof = await adapter.stop_member("task", service.member)
    assert proof["boot_id"] == "boot-1" and proof["drained"]
    assert not service.operations


async def test_pending_stop_waits_for_local_exit_then_remote_proof():
    import asyncio

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    events = []
    member = {
        "id": "member-1",
        "pending_operation": {"request_id": "stop"},
        "current_turn_id": "turn",
    }

    class LifecycleService:
        async def runtime(self, _tid, operation, **_):
            events.append(operation)

    class Execution:
        async def stop_member(self, *_):
            events.append("drain")
            return {"boot_id": "boot", "generation": 1, "drained": True}

    async def writer():
        try:
            await asyncio.Event().wait()
        finally:
            events.append("last checkpoint")

    coordinator = CtfCoordinator(
        cast(Any, None),
        cast(Any, SimpleNamespace()),
        runner=SimpleNamespace(pending_checkpoints={}),
        envd=Execution(),
    )
    coordinator.service = cast(Any, LifecycleService())
    future = asyncio.create_task(writer())
    await asyncio.sleep(0)
    coordinator.active[("task", "member-1")] = (future, {"id": "turn"})
    await coordinator.member_operations("task", {"task": {"ctf_control": {}}, "members": [member]})
    assert events == ["last checkpoint", "drain", "complete_member_operation"]
    assert not coordinator.active


async def test_replacement_callback_runs_only_after_local_checkpoint_and_join():
    import asyncio

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    events = []

    class LifecycleService:
        async def state(self, _):
            return {"task": {"ctf_control": {}}}

        async def runtime(self, *_):
            events.append("recover")

    class Runner:
        envds = {}

        async def retry_checkpoints(self, _, **kwargs):
            events.append("checkpoint retry")

    async def replace(_):
        events.append("archive destroy restore")
        return Env()

    async def writer():
        try:
            await asyncio.Event().wait()
        finally:
            events.append("writer exit")

    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=Runner(), envd_replacement=replace
    )
    coordinator.service = cast(Any, LifecycleService())
    future = asyncio.create_task(writer())
    await asyncio.sleep(0)
    coordinator.active[("task", "member-1")] = (future, {"id": "turn"})
    await coordinator.replace_execution("task")
    assert events == ["writer exit", "checkpoint retry", "archive destroy restore", "recover"]
    assert not coordinator.active


async def test_native_maf_tool_injects_occurrence_context_outside_model_schema():
    from agent_framework import Agent
    from bbx_runtime.ctf.telemetry import ToolObservationMiddleware
    from bbx_runtime.ctf.tools import build_tools
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall

    seen = []
    checkpoint = Checkpoint()

    async def execute_call(tid, member_id, turn, cp, context, arguments):
        seen.append((cp, context.metadata, arguments))
        return "done"

    tools = build_tools(
        cast(Any, Service()),
        "task",
        {"id": "turn", "generation": 1, "token": "t"},
        "teammate",
        envd=SimpleNamespace(execute_call=execute_call),
        member_id="member-1",
        checkpoint_provider=lambda: checkpoint,
    )
    execute = next(item for item in tools if item.name == "execute_command")
    schema = execute.parameters()
    assert "ctx" not in schema.get("properties", {})
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "inspect"}),)),
            ScriptStep(text="complete"),
        ]
    )
    async with Agent(
        client=client,
        tools=[execute],
        middleware=[
            ToolObservationMiddleware(
                cast(Any, Service()), "task", "member-1", {"id": "turn", "generation": 1}
            )
        ],
    ) as agent:
        await agent.run("execute")
    assert len(seen) == 1
    assert seen[0][0] is checkpoint
    assert seen[0][1].get("function_call_occurrence_id")
    assert seen[0][2]["command"] == "inspect"


async def test_restart_continues_destroyed_replacement_before_missing_container_factory():
    from bbx_runtime.ctf.coordinator import CtfCoordinator

    events = []

    class LifecycleService:
        async def state(self, _):
            return {"task": {"ctf_control": {"replacement": {"phase": "destroyed"}}}}

        async def runtime(self, *_):
            events.append("recover")

    class Runner:
        envds = {}

        async def retry_checkpoints(self, _, **kwargs):
            events.append("checkpoints")

    async def factory(_):
        raise AssertionError("Old container does not exist after durable destruction")

    async def replace(_):
        events.append("restore replacement")
        return Env()

    coordinator = CtfCoordinator(
        cast(Any, None),
        cast(Any, SimpleNamespace()),
        runner=Runner(),
        envd_factory=factory,
        envd_replacement=replace,
    )
    coordinator.service = cast(Any, LifecycleService())
    assert isinstance(await coordinator.adapter("task"), CtfExecutionAdapter)
    assert events == ["checkpoints", "restore replacement", "recover"]


async def test_cancelled_runner_retains_failed_checkpoint_until_pending_stop_can_retry(monkeypatch):
    import asyncio
    from pathlib import Path

    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.clients.blackboard import RemoteError
    from bbx_runtime.ctf.runner import CtfRunner

    started = asyncio.Event()
    calls = []
    member = {
        "id": "member-1",
        "role": "teammate",
        "display_name": "Alice",
        "pending_operation": {"kind": "stop"},
    }

    class LocalCheckpoint(Checkpoint):
        revision = 1
        opening_instructions = "role"
        allow_save = False

        async def save(self):
            if not self.allow_save:
                raise RemoteError(503, "checkpoint unavailable")
            calls.append("checkpoint")

    checkpoint = LocalCheckpoint()

    class RuntimeService:
        client: Any

        def __init__(self):
            self.client = self

        async def state(self, _):
            return {
                "task": {
                    "agent_profile": "ctf",
                    "agent_profile_version": 1,
                    "budget": {"max_minutes": 1},
                },
                "members": [member],
            }

        async def get_profile(self, *_):
            return {"profile": load_ctf_profile(Path("profiles/ctf")).model_dump(mode="json")}

        async def runtime(self, _tid, operation, **_):
            calls.append(operation)
            return {}

    class WaitingAgent:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def run(self, *_, **__):
            async def stream():
                started.set()
                await asyncio.Event().wait()
                yield None

            return stream()

    async def load(*_):
        return checkpoint

    monkeypatch.setattr("bbx_runtime.ctf.runner.load_checkpoint", load)
    monkeypatch.setattr("bbx_runtime.ctf.runner.Agent", WaitingAgent)
    runner = CtfRunner(
        cast(Any, RuntimeService()),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: cast(Any, object()),
        envd=SimpleNamespace(stop_member=object()),
    )
    future = asyncio.create_task(
        runner.run("task", member, {"id": "turn", "generation": 1, "token": "t"})
    )
    await started.wait()
    future.cancel()
    with pytest.raises(RemoteError, match="checkpoint unavailable"):
        await future
    assert runner.pending_checkpoints[("task", "member-1")][0] is checkpoint
    checkpoint.allow_save = True
    await runner.retry_checkpoints("task")
    assert not runner.pending_checkpoints
    assert "finish_turn" not in calls


async def test_replacement_checkpoint_flush_does_not_query_unknown_old_execution():
    from bbx_runtime.ctf.runner import CtfRunner

    class OldExecution:
        async def stop_member(self, *_):
            raise AssertionError("Unknown old boot must not be used to settle before replacement")

    service, checkpoint = Service(), Checkpoint()
    runner = CtfRunner(cast(Any, service), cast(Any, SimpleNamespace()), envd=OldExecution())
    runner.pending_checkpoints[("task", "member-1")] = (
        checkpoint,
        {"id": "member-1"},
        {"id": "turn", "generation": 1},
        "answer",
    )
    await runner.retry_checkpoints("task", settle=False)
    assert checkpoint.saved
    assert not runner.pending_checkpoints
    assert not service.operations


async def test_known_generation_drain_pending_does_not_claim_unknown_boot():
    from bbx_runtime.ctf.execution import DrainPending

    class ReapingEnv(Env):
        async def ctf_operation(self, operation, identity):
            return {**identity, "state": "stopping", "drained": False}

    service = Service()
    adapter = Adapter(cast(Any, service), cast(Any, ReapingEnv()))
    with pytest.raises(DrainPending):
        await adapter.stop_member("task", service.member)
    assert not service.operations


async def test_normal_turn_keeps_answer_and_generation_until_remote_drain_succeeds():
    from bbx_runtime.ctf.execution import DrainPending
    from bbx_runtime.ctf.runner import CtfRunner

    class ReapingExecution:
        drained = False

        async def stop_member(self, *_):
            if not self.drained:
                raise DrainPending()
            return {"drained": True}

    service, execution = Service(), ReapingExecution()
    runner = CtfRunner(cast(Any, service), cast(Any, SimpleNamespace()), envd=execution)
    turn = {"id": "turn", "generation": 1}
    await runner.settle("task", service.member, turn, "completed", "final answer")
    assert runner.pending_settlements[("task", "member-1")][3] == "final answer"
    assert not service.operations
    await runner.retry_settlements("task")
    assert not service.operations
    execution.drained = True
    await runner.retry_settlements("task")
    assert not runner.pending_settlements
    assert service.operations[0][0] == "finish_turn"
    assert service.operations[0][1]["answer"] == "final answer"
    assert service.operations[0][1]["generation"] == 1


async def test_recovery_waits_for_process_reaping_without_container_replacement():
    from bbx_runtime.ctf.coordinator import CtfCoordinator
    from bbx_runtime.ctf.execution import DrainPending

    class ReapingExecution:
        drained = False

        async def drain(self, _):
            if not self.drained:
                raise DrainPending()
            return True

    class StateService(Service):
        async def state(self, _):
            return {"task": {"ctf_control": {"phase": "running"}}, "members": []}

    service, execution = StateService(), ReapingExecution()
    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=object(), envd=execution
    )
    coordinator.service = cast(Any, service)
    await coordinator.recover("task")
    assert "task" in coordinator.recovery_pending
    assert not service.operations
    execution.drained = True
    await coordinator.tick("task")
    assert not coordinator.recovery_pending
    assert service.operations[0][0] == "recover"
