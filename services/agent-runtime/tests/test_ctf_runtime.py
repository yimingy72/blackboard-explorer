"""Offline native-MAF CTF mailbox recovery and coordinator admission tests."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from agent_framework import Agent, MessageInjectionMiddleware, tool
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import budget_exhausted
from bbx_runtime.ctf.middleware import PENDING_KEY, MailboxMiddleware
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.ctf.tools import build_tools
from bbx_runtime.session import CheckpointHistoryProvider
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall


class MemoryCtf:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None
        self.rows: list[dict[str, Any]] = []
        self.delivered = set()
        self.generation = 1
        self.lose_response = False
        self.operations = []
        self.observations = []

    def post(self, mid: str, body: str) -> None:
        self.rows.append(
            dict(
                id=mid,
                body=body,
                sender_kind="agent",
                sender_id="lead",
                recipient_id="member-1",
                kind="instruction",
                claim_token=mid,
            )
        )

    async def get_agent_session(self, *_):
        if self.saved is None:
            raise RemoteError(404, "missing")
        return copy.deepcopy(self.saved)

    async def read_evidence(self, _):
        return b""

    async def runtime(self, task_id, operation, **body):
        self.operations.append(operation)
        if body.get("generation", self.generation) != self.generation:
            raise RemoteError(409, "fenced")
        if operation == "record_observation":
            self.observations.append(body)
            return {}
        if operation == "checkpoint_failed":
            return {}
        if operation == "authorize_member":
            return {}
        if operation == "claim_messages":
            return [row for row in self.rows if row["id"] not in self.delivered]
        if operation == "checkpoint":
            revision = self.saved["revision"] if self.saved else 0
            if body["expected_revision"] != revision:
                raise RemoteError(409, "revision")
            self.saved = dict(
                session=copy.deepcopy(body["session"]),
                revision=revision + 1,
                opening_instructions=body["opening_instructions"],
                origin="native",
            )
            self.delivered.update(item["message_id"] for item in body["deliveries"])
            if self.lose_response:
                self.lose_response = False
                raise httpx.ReadError("committed response lost")
            return copy.deepcopy(self.saved)
        raise AssertionError(operation)


TURN = {"id": "turn-1", "generation": 1}


@pytest.mark.parametrize("terminal", ["incomplete", "failed"])
async def test_runner_records_failed_model_stream_without_tools_retries_or_invented_usage(terminal):
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from agent_framework import ChatResponseUpdate, Content
    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.runner import CtfRunner
    from bbx_runtime.model_errors import ModelStreamError
    from bbx_runtime.models import _require_complete_stream
    from bbx_runtime.testing.scripted_client import ScriptUsage

    profile = load_ctf_profile(Path("profiles/ctf"))

    class RuntimeService(MemoryCtf):
        def __init__(self):
            super().__init__()
            self.client = self
            self.bills = []

        async def state(self, _):
            return {
                "task": {
                    "agent_profile": "ctf",
                    "agent_profile_version": 1,
                    "budget": {"max_minutes": 1},
                }
            }

        async def get_profile(self, *_):
            return {"profile": profile.model_dump(mode="json")}

        async def runtime(self, task_id, operation, **body):
            if operation == "bill_usage":
                self.bills.append(body)
                return {}
            return await super().runtime(task_id, operation, **body)

    class TerminalClient(ScriptedChatClient):
        failed_requests = 0

        def _inner_get_response(self, **kwargs):
            if self._index == 0:
                return super()._inner_get_response(**kwargs)
            self.failed_requests += 1

            async def updates():
                yield ChatResponseUpdate(
                    role="assistant",
                    contents=[
                        Content.from_function_call(
                            "partial-call", "execute_command", arguments={"command": "do not run"}
                        )
                    ],
                )
                yield ChatResponseUpdate(
                    raw_representation=SimpleNamespace(
                        type=f"response.{terminal}",
                        response=SimpleNamespace(
                            error=SimpleNamespace(code="invalid_request", message="secret payload"),
                            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                        ),
                    )
                )

            return _require_complete_stream(self._build_response_stream(updates()), responses=True)

    class FakeEnvd:
        calls = []

        async def execute(self, *_args):
            self.calls.append(_args)
            return "saved result"

    service, envd = RuntimeService(), FakeEnvd()
    service.post("assignment", "synthetic instruction")
    client = TerminalClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("execute_command", {"command": "first command"}),),
                usage=ScriptUsage(completion_tokens=193, prompt_cache_miss_tokens=4169),
            )
        ]
    )
    runner = CtfRunner(
        cast(CtfClient, service),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: client,
        envd=envd,
    )
    with pytest.raises(ModelStreamError):
        await runner.run(
            "task",
            {"id": "member-1", "display_name": "Alice", "role": "teammate"},
            {**TURN, "token": "fake"},
        )
    errors = [row for row in service.observations if row["kind"] == "model_error"]
    assert len(errors) == 1
    diagnostic = errors[0]["body"]
    assert diagnostic["exception_type"] == "ModelStreamError"
    assert diagnostic["event_type"] == f"response.{terminal}"
    assert diagnostic["incomplete_reason"] == "max_output_tokens"
    assert diagnostic["failure_phase"] == "stream_completion"
    assert diagnostic["transient"] is False
    assert "secret payload" not in json.dumps(diagnostic)
    assert "do not run" not in json.dumps(diagnostic)
    assert "synthetic instruction" not in json.dumps(diagnostic)
    assert errors[0]["request_id"]
    assert client.failed_requests == 1
    assert len(envd.calls) == 1
    assert len(service.bills) == 1
    assert service.bills[0]["usage"]["output_tokens"] == 193
    assert sum(row["kind"] == "model_output" for row in service.observations) == 1
    assert "finish_turn" not in service.operations


async def run_native(service, client, tools=()):
    cp = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "CTF role")
    mailbox = MailboxMiddleware(cast(CtfClient, service), cp, TURN, 10)
    async with Agent(
        client=client,
        instructions="CTF role",
        tools=list(tools),
        context_providers=[CheckpointHistoryProvider(cp)],
        middleware=[mailbox, MessageInjectionMiddleware()],
        require_per_service_call_history_persistence=True,
    ) as agent:
        stream = agent.run([], session=cp.session, stream=True)
        async for _ in stream:
            pass
        await stream.get_final_response()
    return cp


async def test_native_injection_at_tool_boundary_and_same_session_restore():
    service = MemoryCtf()
    service.post("first", "original task")

    @tool
    async def pause() -> str:
        """Queue input while this turn is busy."""
        service.post("second", "busy instruction")
        service.post("third", "another instruction")
        return "tool result"

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("pause"),), expect_contains="original task"),
            ScriptStep(text="done", expect_contains="busy instruction"),
        ]
    )
    cp = await run_native(service, client, [pause])
    assert service.delivered == {"first", "second", "third"}
    session_id = cp.session.session_id
    service.post("fourth", "continue")
    restored = await run_native(
        service, ScriptedChatClient([ScriptStep(text="continued", expect_contains="original task")])
    )
    assert restored.session.session_id == session_id
    history = restored.session.state["in_memory"]["messages"]
    assert [m.message_id for m in history if m.role == "user"] == [
        "first",
        "second",
        "third",
        "fourth",
    ]
    assert history[0].additional_properties["ctf_source"]["sender_id"] == "lead"


async def test_pending_checkpoint_is_not_delivery_and_lost_response_reconciles():
    service = MemoryCtf()
    service.post("first", "original task")
    cp = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "CTF role")
    await MailboxMiddleware(cast(CtfClient, service), cp, TURN, 10).reconcile()
    await cp.save()
    assert not service.delivered
    assert cp.session.state[PENDING_KEY]
    service.lose_response = True
    restored = await run_native(service, ScriptedChatClient([ScriptStep(text="ok")]))
    assert service.delivered == {"first"}
    assert (
        len([m for m in restored.session.state["in_memory"]["messages"] if m.message_id == "first"])
        == 1
    )


async def test_drained_input_reappears_after_failed_stream_even_with_live_claim():
    service = MemoryCtf()
    service.post("first", "retry me")
    with pytest.raises(AssertionError, match="ran out of steps"):
        await run_native(service, ScriptedChatClient([]))
    assert not service.delivered
    restored = await run_native(
        service, ScriptedChatClient([ScriptStep(text="ok", expect_contains="retry me")])
    )
    assert service.delivered == {"first"}
    assert len(restored.session.state["in_memory"]["messages"]) == 2


async def test_stale_generation_cannot_checkpoint():
    service = MemoryCtf()
    cp = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "CTF role")
    service.generation = 2
    with pytest.raises(RemoteError, match="fenced"):
        await cp.save()


def test_budget_includes_idle_elapsed_time():
    task: dict[str, Any] = dict(
        budget={"max_cost": "1", "max_minutes": 1},
        usage={},
        active_seconds=0,
        active_since=datetime.now(UTC) - timedelta(seconds=61),
    )
    assert budget_exhausted(task)
    task["active_since"] = None
    task["usage"] = {"cost": "1"}
    assert budget_exhausted(task)


def test_role_tool_permissions_and_explicit_fake_execution():
    service = cast(CtfClient, MemoryCtf())
    turn = {**TURN, "token": "fake"}
    lead = {t.name for t in build_tools(service, "task", turn, "lead")}
    teammate = {t.name for t in build_tools(service, "task", turn, "teammate")}
    board = {
        "list_challenges",
        "get_challenge",
        "create_challenge",
        "update_challenge",
        "list_records",
        "append_record",
        "register_artifact",
        "request_help",
    }
    assert lead - board == {
        "list_members",
        "send_message",
        "create_teammate",
        "finish_task",
        "stop_teammate",
        "resume_teammate",
        "remove_teammate",
        "record_candidate",
        "set_verification_required",
        "confirm_messages",
    }
    assert teammate == {"list_members", "send_message", "record_candidate"} | board
    assert board <= lead
    assert "execute_command" not in lead
    assert "execute_command" in {
        t.name for t in build_tools(service, "task", turn, "lead", envd=object())
    }


@pytest.mark.parametrize("allow_execute", [True, False])
async def test_runner_uses_explicit_fake_envd_and_settles_after_checkpoint(allow_execute):
    from pathlib import Path
    from types import SimpleNamespace

    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.runner import CtfRunner

    profile = load_ctf_profile(Path("profiles/ctf"))
    if not allow_execute:
        profile.worker_tools["teammate"].builtin.remove("execute_command")

    class RuntimeService(MemoryCtf):
        def __init__(self):
            super().__init__()
            self.client = self
            self.settlements = []
            self.billed = set()

        async def state(self, _):
            return {
                "task": {
                    "agent_profile": "ctf",
                    "agent_profile_version": 1,
                    "budget": {"max_minutes": 1},
                }
            }

        async def get_profile(self, *_):
            return {"profile": profile.model_dump(mode="json")}

        async def runtime(self, task_id, operation, **body):
            if operation == "finish_turn":
                assert self.saved is not None
                self.settlements.append(body)
                return body
            if operation == "bill_usage":
                self.billed.add(body["request_id"])
                return {}
            return await super().runtime(task_id, operation, **body)

    class FakeEnvdAdapter:
        def __init__(self):
            self.calls = []

        async def execute(self, tid, member_id, command):
            self.calls.append((tid, member_id, command))
            return "simulated command result"

    service, envd = RuntimeService(), FakeEnvdAdapter()
    service.post("assignment", "inspect the fake command")
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "inspect"}),)),
            ScriptStep(text="fake work complete", expect_contains="simulated command result"),
        ]
    )
    if not allow_execute:
        client = ScriptedChatClient([ScriptStep(text="fake work complete")])
    runner = CtfRunner(
        cast(CtfClient, service),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: client,
        envd=envd,
    )
    await runner.run(
        "task",
        {"id": "member-1", "display_name": "Alice", "role": "teammate"},
        {**TURN, "token": "fake"},
    )
    assert envd.calls == ([("task", "member-1", "inspect")] if allow_execute else [])
    exposed = {tool.name for tool in client.received_options[0].get("tools", [])}
    assert ("execute_command" in exposed) is allow_execute
    assert service.settlements[0]["answer"] == "fake work complete"
    assert service.settlements[0]["end_reason"] == "completed"
    from bbx_runtime.ctf.budget import ctf_max_output_tokens

    assert (
        client.received_options[0]["max_tokens"] == ctf_max_output_tokens(profile.model) == 393216
    )
    assert service.delivered == {"assignment"}
    kinds = [row["kind"] for row in service.observations]
    assert kinds.count("initial_context") == 1
    assert "turn_context" in kinds and "model_output" in kinds
    assert ("tool_call" in kinds) is allow_execute
    assert ("tool_result" in kinds) is allow_execute
    assert all(
        row["turn_id"] == TURN["id"] and row["generation"] == 1 for row in service.observations
    )


async def test_context_limit_settlement_enqueues_one_continuation():
    from types import SimpleNamespace

    from bbx_runtime.ctf.runner import CtfRunner

    operations = []

    class Service:
        async def runtime(self, _task_id, operation, **_body):
            operations.append(operation)
            return {}

    runner = CtfRunner(cast(CtfClient, Service()), cast(Any, SimpleNamespace()))
    member = {"id": "member-1"}
    turn = {"id": "turn-1", "generation": 1, "purpose": "execution"}
    assert await runner.settle("task", member, turn, "context_limit", "")
    assert operations == ["finish_turn", "enqueue_continuation"]


async def test_context_limit_continuation_does_not_override_user_stop_or_budget():
    from types import SimpleNamespace

    from bbx_runtime.ctf.runner import CtfRunner

    class Service:
        async def runtime(self, _task_id, operation, **_body):
            if operation == "enqueue_continuation":
                raise RemoteError(409, "Task is not running")
            return {}

    runner = CtfRunner(cast(CtfClient, Service()), cast(Any, SimpleNamespace()))
    assert await runner.settle(
        "task", {"id": "member-1"}, {"id": "turn-1", "generation": 1}, "context_limit", ""
    )


async def test_running_coordinator_claims_queued_work_and_budget_closes_first():
    from types import SimpleNamespace

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    class Runner:
        async def run(self, *_):
            await asyncio.Event().wait()

    class Service:
        def __init__(self, exhausted=False):
            self.exhausted = exhausted
            self.operations = []

        async def state(self, _):
            return {
                "task": {
                    "ctf_control": {"phase": "running"},
                    "budget": {"max_cost": "1", "max_minutes": 10},
                    "usage": {"cost": "1"} if self.exhausted else {},
                    "active_seconds": 0,
                    "active_since": None,
                },
                "members": [{"id": "lead", "lifecycle": "active"}],
            }

        async def runtime(self, _task_id, operation, **_body):
            self.operations.append(operation)
            if operation == "claim_turn":
                return {"id": "turn", "generation": 1, "agent_id": "lead"}
            return {}

    queued = Service()
    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=Runner(), envd=None
    )
    coordinator.service = cast(CtfClient, queued)
    await coordinator.tick("task")
    assert "claim_turn" in queued.operations
    await coordinator.close()

    exhausted = Service(exhausted=True)
    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=Runner(), envd=None
    )
    coordinator.service = cast(CtfClient, exhausted)
    await coordinator.tick("task")
    assert exhausted.operations == ["request_finish"]
    await coordinator.close()


async def test_renew_409_after_runner_settles_turn_is_an_expected_race():
    from types import SimpleNamespace

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    class Service:
        def __init__(self):
            self.settled = False
            self.operations = []

        async def state(self, _):
            return {
                "task": {
                    "ctf_control": {"phase": "running"},
                    "budget": {"max_cost": "1", "max_minutes": 10},
                    "usage": {},
                    "active_seconds": 0,
                    "active_since": None,
                },
                "members": [
                    {
                        "id": "lead",
                        "lifecycle": "active",
                        "current_turn_id": None if self.settled else "turn-1",
                    }
                ],
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "finished" if self.settled else "running",
                    }
                ],
            }

        async def runtime(self, _task_id, operation, **_body):
            self.operations.append(operation)
            if operation == "renew_turn":
                self.settled = True
                raise RemoteError(409, "CTF turn is not running")
            return None

    service = Service()
    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=object(), envd=None
    )
    coordinator.service = cast(CtfClient, service)
    future = asyncio.create_task(asyncio.Event().wait())
    coordinator.active[("task", "lead")] = (future, {"id": "turn-1", "generation": 1})
    try:
        assert await coordinator.tick("task") is True
        assert service.operations == ["renew_turn"]
    finally:
        future.cancel()
        await asyncio.gather(future, return_exceptions=True)
        await coordinator.close()


async def test_coordinator_stops_admission_at_the_exact_budget_cap():
    from types import SimpleNamespace

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    class Service:
        operations = []

        async def state(self, _):
            return {
                "task": {
                    "ctf_control": {"phase": "running"},
                    "budget": {"max_cost": "0.40", "max_minutes": 10},
                    "usage": {"cost": "0.40"},
                    "active_seconds": 0,
                    "active_since": None,
                },
                "members": [
                    {"id": "lead", "lifecycle": "active", "run_state": "idle"},
                    {"id": "member-1", "lifecycle": "active", "run_state": "idle"},
                    {"id": "member-2", "lifecycle": "active", "run_state": "idle"},
                ],
            }

        async def runtime(self, _task_id, operation, **_body):
            self.operations.append(operation)
            return {}

    service = Service()
    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=object(), envd=None
    )
    coordinator.service = cast(CtfClient, service)
    await coordinator.tick("task")
    assert service.operations == ["request_finish"]
    await coordinator.close()


def test_ctf_reservation_requires_a_real_context_bound():
    from pathlib import Path

    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.budget import ctf_call_reservation, ctf_max_output_tokens
    from bbx_runtime.models import ctf_model_run_options

    model = load_ctf_profile(Path("profiles/ctf")).model
    assert ctf_call_reservation(model, context_tokens=128000) is None
    price = model.price.model_copy(update={"billing_mode": "fixed"})
    bounded = model.model_copy(update={"context_window": 128000, "price": price})
    assert (
        ctf_model_run_options(bounded).get("max_tokens") == ctf_max_output_tokens(bounded) == 393216
    )
    assert ctf_call_reservation(bounded, context_tokens=128000) == Decimal("3.401728")
    assert ctf_call_reservation(bounded, context_tokens=128000) == (
        Decimal(128000) * Decimal("2.00")
        + Decimal(ctf_max_output_tokens(bounded)) * Decimal("8.00")
    ) / Decimal(1_000_000)
    assert ctf_call_reservation(bounded, context_tokens=128000, max_output_tokens=8192) == Decimal(
        "0.321536"
    )


@pytest.mark.parametrize("provider", ["deepseek", "openai_responses", "openai_compatible"])
@pytest.mark.parametrize(
    "model_id",
    ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro"],
)
def test_ctf_deepseek_maximum_uses_exact_model_ids_across_transports(provider, model_id):
    from pathlib import Path

    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.budget import ctf_max_output_tokens
    from bbx_runtime.models import ctf_model_run_options

    model = load_ctf_profile(Path("profiles/ctf")).model.model_copy(
        update={"provider": provider, "model": model_id}
    )
    assert ctf_max_output_tokens(model) == 393216
    assert ctf_model_run_options(model).get("max_tokens") == 393216


@pytest.mark.parametrize("model_id", ["gpt-test", "deepseek-chat", "deepseek-flash-custom"])
def test_ctf_other_models_keep_fallback_output_bound(model_id):
    from pathlib import Path

    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.budget import CTF_MAX_OUTPUT_TOKENS, ctf_max_output_tokens
    from bbx_runtime.models import ctf_model_run_options

    model = load_ctf_profile(Path("profiles/ctf")).model.model_copy(
        update={"provider": "openai_responses", "model": model_id}
    )
    assert ctf_max_output_tokens(model) == CTF_MAX_OUTPUT_TOKENS == 8192
    assert ctf_model_run_options(model).get("max_tokens") == 8192


async def test_model_error_observation_failure_preserves_original_exception():
    from pathlib import Path
    from types import SimpleNamespace

    from agent_framework import ChatContext
    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.runner import ModelErrorMiddleware
    from bbx_runtime.model_errors import ModelStreamError

    class UnavailableObservations:
        async def runtime(self, *_args, **_kwargs):
            raise OSError("synthetic object store failure")

    middleware = ModelErrorMiddleware(
        cast(CtfClient, UnavailableObservations()),
        "task",
        "member-1",
        TURN,
        load_ctf_profile(Path("profiles/ctf")).model,
    )
    original = ModelStreamError("invalid_response", event_type="response.incomplete")

    async def call_next():
        raise original

    with pytest.raises(ModelStreamError) as failure:
        await middleware.process(cast(ChatContext, SimpleNamespace(messages=[])), call_next)
    assert failure.value is original


@pytest.mark.parametrize(
    ("metadata", "summary_fragment"),
    [
        ({"incomplete_reason": "max_output_tokens", "payload": "secret payload"}, "输出上限"),
        ({"category": "content_filter", "payload": "secret payload"}, "内容审核"),
        ({"category": "unknown", "payload": "secret payload"}, "内部错误"),
        ("max_output_tokens secret payload", "内部错误"),
    ],
)
def test_model_failure_summary_uses_only_fixed_safe_metadata(metadata, summary_fragment, caplog):
    import json
    from uuid import UUID

    from bbx_runtime.ctf.coordinator import failure_diagnostic

    original = RuntimeError("secret exception and request body")
    vars(original)["bbx_model_error"] = metadata
    try:
        raise original
    except RuntimeError as error:
        diagnostic = failure_diagnostic("model_turn", error)

    assert summary_fragment in diagnostic["summary"]
    assert diagnostic["error_type"] == "RuntimeError"
    assert diagnostic["phase"] == "model_turn"
    assert diagnostic["occurred_at"]
    assert UUID(diagnostic["correlation_id"])
    assert diagnostic["correlation_id"] in caplog.text
    assert "phase=model_turn" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "test_model_failure_summary_uses_only_fixed_safe_metadata" in caplog.text
    persisted = json.dumps(diagnostic) + caplog.text
    assert "secret exception" not in persisted
    assert "request body" not in persisted
    assert "secret payload" not in persisted


async def test_coordinator_persists_start_failure_instead_of_requeueing():
    from types import SimpleNamespace

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    class Service:
        operations = []

        async def state(self, _):
            return {"task": {"status": "provisioning", "ctf_control": {"phase": "provisioning"}}}

        async def runtime(self, _task_id, operation, **_body):
            self.operations.append(operation)
            return {}

    async def fail(_):
        raise RuntimeError("fake worker unavailable")

    service = Service()
    coordinator = CtfCoordinator(
        cast(Any, None),
        cast(Any, SimpleNamespace()),
        runner=object(),
        envd_factory=fail,
    )
    coordinator.service = cast(CtfClient, service)
    with pytest.raises(RuntimeError, match="fake worker unavailable"):
        await coordinator.run("task")
    assert service.operations == ["fail_start"]


async def test_future_failure_persists_safe_diagnostic_and_completes_cleanup(caplog):
    from types import SimpleNamespace

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    class Service:
        def __init__(self):
            self.phase = "running"
            self.operations = []
            self.conclusion = None

        async def state(self, _):
            return {
                "task": {
                    "ctf_control": {"phase": self.phase, "unresolved_checkpoints": []},
                    "budget": {"max_cost": "10", "max_minutes": 10},
                    "usage": {"cost": "0"},
                    "active_seconds": 0,
                    "active_since": None,
                },
                "members": [],
            }

        async def runtime(self, _task_id, operation, **body):
            self.operations.append(operation)
            if operation == "request_finish":
                self.phase = "closing"
                self.conclusion = body["conclusion"]
            elif operation == "finalize_close":
                self.phase = "closed"
            return {}

    class Envd:
        async def drain(self, _):
            return True

    service = Service()
    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=object(), envd=Envd()
    )
    coordinator.service = cast(CtfClient, service)

    async def fail_turn() -> None:
        raise RuntimeError("model request secret should not be logged")

    future = asyncio.create_task(fail_turn())
    await asyncio.sleep(0)
    coordinator.active[("task", "lead")] = (future, {"id": "turn-1", "generation": 1})

    with caplog.at_level("ERROR", logger="bbx_runtime.ctf.coordinator"):
        assert await coordinator.tick("task") is True
    assert service.conclusion is not None
    conclusion = service.conclusion
    assert conclusion["end_reason"] == "system_failure"
    failure = conclusion["failure"]
    assert failure["error_type"] == "RuntimeError"
    assert failure["phase"] == "turn"
    assert failure["correlation_id"]
    assert failure["summary"] != "system_failure"
    assert "model request secret should not be logged" not in caplog.text

    assert await coordinator.tick("task") is False
    assert service.operations == ["request_finish", "recover", "finalize_close"]
    await coordinator.close()


async def test_tick_failure_persists_reference_and_finishes_drain(caplog):
    from types import SimpleNamespace

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    class Service:
        def __init__(self):
            self.phase = "running"
            self.state_calls = 0
            self.operations = []
            self.conclusion = None

        async def state(self, _):
            self.state_calls += 1
            if self.state_calls == 2:
                raise RuntimeError("untrusted model payload must stay out of diagnostics")
            return {
                "task": {
                    "ctf_control": {"phase": self.phase, "unresolved_checkpoints": []},
                    "budget": {"max_cost": "10", "max_minutes": 10},
                    "usage": {"cost": "0"},
                    "active_seconds": 0,
                    "active_since": None,
                },
                "members": [],
            }

        async def runtime(self, _task_id, operation, **body):
            self.operations.append(operation)
            if operation == "request_finish":
                self.phase = "closing"
                self.conclusion = body["conclusion"]
            elif operation == "finalize_close":
                self.phase = "closed"
            return {}

    class Envd:
        async def drain(self, _):
            return True

    service = Service()
    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=object(), envd=Envd(), poll_seconds=0
    )
    coordinator.service = cast(CtfClient, service)
    with caplog.at_level("ERROR", logger="bbx_runtime.ctf.coordinator"):
        await coordinator.run("task")
    assert service.conclusion is not None
    failure = service.conclusion["failure"]
    assert failure["error_type"] == "RuntimeError"
    assert failure["phase"] == "tick"
    assert failure["correlation_id"]
    assert "untrusted model payload" not in caplog.text
    assert service.operations == ["start", "request_finish", "recover", "finalize_close"]
    await coordinator.close()


async def test_closing_waits_for_drain_and_never_claims_new_work():
    from types import SimpleNamespace

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    class Control:
        def __init__(self):
            self.operations = []

        async def state(self, _):
            return {"task": {"ctf_control": {"phase": "closing"}}, "members": []}

        async def runtime(self, _tid, operation, **_):
            self.operations.append(operation)

    class FakeEnvdAdapter:
        drained = False

        async def drain(self, _):
            return self.drained

    service, envd = Control(), FakeEnvdAdapter()
    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=object(), envd=envd
    )
    coordinator.service = cast(CtfClient, service)
    assert await coordinator.tick("task") is True
    assert "finalize_close" not in service.operations
    envd.drained = True
    assert await coordinator.tick("task") is False
    assert service.operations == ["recover", "finalize_close"]
    await coordinator.close()


async def test_recover_joins_old_writer_before_drain_and_generation_takeover():
    import asyncio
    from types import SimpleNamespace

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    events = []

    class Service:
        async def state(self, _):
            return {"task": {"ctf_control": {}}, "members": []}

        async def runtime(self, *_):
            events.append("recover")

    class FakeEnvdAdapter:
        async def drain(self, _):
            events.append("drain")
            return True

    async def old_writer():
        try:
            await asyncio.Event().wait()
        finally:
            events.append("writer exited")

    coordinator = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=object(), envd=FakeEnvdAdapter()
    )
    coordinator.service = cast(CtfClient, Service())
    future = asyncio.create_task(old_writer())
    await asyncio.sleep(0)
    coordinator.active[("task", "lead")] = (future, TURN)
    await coordinator.recover("task")
    assert events == ["writer exited", "drain", "recover"]
    assert not coordinator.active


async def test_unconfirmed_checkpoint_blocks_goal_claimed_finalization():
    from types import SimpleNamespace

    from bbx_runtime.ctf.coordinator import CtfCoordinator

    operations = []

    class Service:
        async def state(self, _):
            return {
                "task": {
                    "ctf_control": {"phase": "closing", "unresolved_checkpoints": ["turn"]},
                    "ctf_conclusion": {"end_reason": "goal_claimed"},
                },
                "members": [],
            }

        async def runtime(self, _tid, operation, **_):
            operations.append(operation)

    coordinator = CtfCoordinator(cast(Any, None), cast(Any, SimpleNamespace()), runner=object())
    coordinator.service = cast(CtfClient, Service())
    assert await coordinator.tick("task") is True
    assert "finalize_close" not in operations
    assert "recover" not in operations


async def test_bounded_model_view_preserves_full_persistent_history():
    from pathlib import Path

    from agent_framework import Content, Message
    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.middleware import CtfHistoryProvider

    service = MemoryCtf()
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    old = Message(role="user", message_id="old", contents=[Content.from_text("x" * 2000)])
    final = Message(role="assistant", contents=[Content.from_text("old final")])
    recent = Message(role="user", message_id="recent", contents=[Content.from_text("new")])
    checkpoint.session.state["in_memory"] = {"messages": [old, final, recent]}
    provider = CtfHistoryProvider(
        cast(CtfClient, service),
        checkpoint,
        TURN,
        load_ctf_profile(Path("profiles/ctf")).model,
        1000,
    )
    view = await provider.get_messages(
        checkpoint.session.session_id, state=checkpoint.session.state["in_memory"]
    )
    assert view == [recent]
    assert checkpoint.session.state["in_memory"]["messages"] == [old, final, recent]
    assert checkpoint.session.state["ctf_history_view"]["excluded_before"] == 2


async def test_bounded_model_view_can_resume_after_a_complete_tool_batch():
    from pathlib import Path

    from agent_framework import Content, Message
    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.middleware import CtfHistoryProvider

    service = MemoryCtf()
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    call = Message(
        role="assistant",
        contents=[Content.from_function_call("call-1", "inspect", arguments={})],
    )
    result = Message(
        role="tool",
        contents=[Content.from_function_result("call-1", result="x" * 2000)],
    )
    recent = Message(role="user", message_id="recent", contents=[Content.from_text("new")])
    checkpoint.session.state["in_memory"] = {"messages": [call, result, recent]}
    provider = CtfHistoryProvider(
        cast(CtfClient, service),
        checkpoint,
        TURN,
        load_ctf_profile(Path("profiles/ctf")).model,
        1000,
    )
    view = await provider.get_messages(
        checkpoint.session.session_id, state=checkpoint.session.state["in_memory"]
    )
    assert view == [recent]
    assert checkpoint.session.state["ctf_history_view"]["excluded_before"] == 2


async def test_context_limit_view_uses_real_persisted_tool_batches():
    from pathlib import Path

    from agent_framework import Message, tool
    from bbx_contracts.ctf import load_ctf_profile
    from bbx_runtime.ctf.middleware import CtfHistoryProvider, mailbox_message

    service = MemoryCtf()
    service.post("first", "original task")

    @tool
    async def inspect() -> str:
        """Return a large deterministic observation for the scripted model."""
        return "observation " * 500

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("inspect"),)),
            ScriptStep(calls=(ScriptToolCall("inspect"),)),
        ]
    )
    with pytest.raises(AssertionError, match="ran out of steps"):
        await run_native(service, client, [inspect])

    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    messages = checkpoint.session.state["in_memory"]["messages"]
    assert sum(message.role == "assistant" for message in messages) == 2
    assert sum(message.role == "tool" for message in messages) == 2
    messages.append(
        Message(
            role="user",
            message_id="continuation",
            contents=[mailbox_message({"id": "continuation", "body": "continue"}).contents[0]],
        )
    )
    provider = CtfHistoryProvider(
        cast(CtfClient, service),
        checkpoint,
        TURN,
        load_ctf_profile(Path("profiles/ctf")).model,
        1000,
    )
    view = await provider.get_messages(
        checkpoint.session.session_id, state=checkpoint.session.state["in_memory"]
    )
    assert [message.message_id for message in view if message.role == "user"] == ["continuation"]
    assert checkpoint.session.state["ctf_history_view"]["excluded_before"] > 0


async def test_failed_checkpoint_marks_durable_barrier_after_reconciliation_fails():
    class FailingStore(MemoryCtf):
        async def runtime(self, task_id, operation, **body):
            if operation == "checkpoint":
                raise RemoteError(503, "storage unavailable")
            return await super().runtime(task_id, operation, **body)

    service = FailingStore()
    cp = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    with pytest.raises(RemoteError):
        await cp.save()
    assert "checkpoint_failed" in service.operations
    assert not service.delivered


async def test_retry_checkpoint_flushes_usage_before_settlement():
    from types import SimpleNamespace

    from bbx_runtime.ctf.runner import CtfRunner

    operations = []

    class Service:
        async def runtime(self, _tid, operation, **_body):
            operations.append(operation)

    class Checkpoint:
        session = SimpleNamespace(
            state={"ctf_usage_outbox": [{"request_id": "usage", "usage": {}}]}
        )

        async def save(self):
            operations.append("checkpoint")

    runner = CtfRunner(cast(CtfClient, Service()), cast(Any, SimpleNamespace()))
    runner.pending_checkpoints[("task", "member-1")] = (
        Checkpoint(),
        {"id": "member-1"},
        TURN,
        "saved answer",
    )
    await runner.retry_checkpoints("task")
    assert operations == ["checkpoint", "bill_usage", "checkpoint", "finish_turn"]
    assert not runner.pending_checkpoints
