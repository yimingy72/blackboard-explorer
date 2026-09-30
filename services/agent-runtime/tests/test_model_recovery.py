"""Recovery restarts MAF, preserves durable tool history, and bounds outages."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import httpx2
import pytest
from agent_framework import Agent, AgentSession, ChatResponseUpdate, Content, Message, tool
from bbx_blackboard.domain import BoardState
from bbx_blackboard.domain import decide as board_decide
from bbx_contracts.models import Params, WorkerTools
from bbx_contracts.profile import load_profile
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient, RemoteError
from bbx_runtime.context import RunContext
from bbx_runtime.execenv import ExecEnvHandle, ExecEnvManager
from bbx_runtime.middleware import BoardSyncMiddleware
from bbx_runtime.models import close_model_client, make_client
from bbx_runtime.recovery import ModelRecoveryGate, RecoveryExhausted
from bbx_runtime.runner import AgentRunner
from bbx_runtime.session import _unpaired_calls
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from openai import APIStatusError, AsyncOpenAI

RECEIPT = '{"accepted":true,"data":{"posted":[],"excluded":[]}}'
CONNECTION = {"category": "connection", "transient": True}


class RecoveryService:
    def __init__(self, task_type: str = "derive") -> None:
        self.profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
        self.profile.worker_tools["explore"] = WorkerTools(
            builtin=["post_fact", "post_intent", "release"]
        )
        self.board = {
            "task": {
                "status": "running",
                "goal": "Check results",
                "acceptance": [{"id": "A1", "desc": "Check the result"}],
                "acceptance_state": {},
                "agent_profile": "default",
                "agent_profile_version": 1,
                "params": Params().model_dump(),
                "budget": {"max_minutes": 10},
            },
            "agents": {
                "agent-1": {
                    "id": "agent-1",
                    "task_type": task_type,
                    "status": "running",
                    "intent_id": "I1",
                    "derive_round": 2,
                    "last_seen_version": 0,
                    "steps": 0,
                }
            },
            "facts": {},
            "intents": {
                "I1": {
                    "id": "I1",
                    "status": "claimed",
                    "statement": "Check results",
                    "based_on": [],
                }
            },
        }
        self.saved: dict[str, Any] | None = None
        self.deliveries: list[dict[str, str]] = []
        self.messages: list[dict[str, str]] = []
        self.claims: list[str] = []
        self.events_log: list[dict[str, Any]] = []
        self.beats: list[dict[str, Any]] = []
        self.finish_agent = AsyncMock()
        self.record_agent_trace = AsyncMock()
        self.put_error: Exception | None = None
        self.heartbeat_error: Exception | None = None

    async def state(self, _task_id):
        return deepcopy(self.board)

    async def get_profile(self, *_args):
        return self.profile.model_dump()

    def with_token(self, _token):
        return self

    async def events(self, _task_id, *, since=0, **_kwargs):
        return [event for event in self.events_log if event["version"] > since]

    async def snapshot(self, *_args, **_kwargs):
        return "Current board"

    async def get_agent_session(self, *_args):
        if self.saved is None:
            raise RemoteError(404, "missing")
        return deepcopy(self.saved)

    async def put_agent_session(self, _task_id, _agent_id, **kwargs):
        if self.put_error is not None:
            raise self.put_error
        revision = self.saved["revision"] if self.saved else 0
        if kwargs["expected_revision"] != revision:
            raise RemoteError(409, "revision conflict")
        self.saved = {
            key: json.loads(json.dumps(kwargs[key]))
            for key in ("session", "opening_instructions", "origin")
        }
        self.saved["revision"] = revision + 1
        self.deliveries.extend(kwargs["deliveries"])
        for delivery in kwargs["deliveries"]:
            message = next(item for item in self.messages if item["id"] == delivery["id"])
            assert message["status"] == "processing"
            assert delivery["claim_token"] == message["claim_token"]
            message["status"] = "delivered"
        return deepcopy(self.saved)

    async def agent_messages(self, *_args, status):
        return {"messages": [deepcopy(item) for item in self.messages if item["status"] == status]}

    async def claim_agent_message(self, _task_id, _agent_id, message_id, _mode):
        message = next(item for item in self.messages if item["id"] == message_id)
        assert message["status"] == "queued"
        message["status"] = "processing"
        message["claim_token"] = f"claim-{message_id}"
        self.claims.append(message_id)
        return {"message": deepcopy(message), "claim_token": message["claim_token"]}

    async def heartbeat(self, _task_id, _agent_id, **kwargs):
        if self.heartbeat_error:
            raise self.heartbeat_error
        self.beats.append(kwargs)
        agent = self.board["agents"]["agent-1"]
        agent["steps"] += kwargs["steps"]
        agent["last_seen_version"] = kwargs["last_seen_version"]
        if agent["task_type"] == "derive":
            assert kwargs["expected_derive_round"] == 2
        return {}

    async def record_tool_call(self, *_args, **_kwargs):
        return []


class RecoveryObjects:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    async def put(self, uri, data, **_kwargs):
        self.files[uri] = data


class InterruptedClient(ScriptedChatClient):
    def __init__(self, steps, faults, *, partial_tool=False):
        super().__init__(steps)
        self.faults = faults
        self.partial_tool = partial_tool
        self.failed = asyncio.Event()

    def _inner_get_response(self, **kwargs):
        index = self._index
        response = super()._inner_get_response(**kwargs)
        if index not in self.faults:
            return response
        error = self.faults[index]

        async def updates():
            self.failed.set()
            contents = [Content.from_text("POISON{")]
            if self.partial_tool:
                contents.append(
                    Content.from_function_call("broken-call", "step", arguments='{"x":')
                )
            yield ChatResponseUpdate(role="assistant", contents=contents)
            raise error

        return self._build_response_stream(updates())


@pytest.fixture
def fast_recovery(monkeypatch):
    monkeypatch.setattr("bbx_runtime.recovery.RECOVERY_BACKOFF", (0.0, 0.0))


def runner_for(service):
    objects = RecoveryObjects()
    runner = AgentRunner(
        Settings.model_construct(),
        cast(BlackboardClient, service),
        cast(ObjectStore, objects),
        cast(ExecEnvManager, Mock()),
    )
    return runner, objects


async def invoke(runner, client, task_type="derive"):
    return await runner.run_agent(
        "task",
        "agent-1",
        task_type,
        agent_token="issued",
        client=client,
        handle=ExecEnvHandle(
            task_id=uuid4(), container_id="envd", name="envd", base_url="http://envd", token="token"
        ),
    )


async def test_stream_fragments_are_discarded_and_claimed_input_is_delivered_once(fast_recovery):
    service = RecoveryService()
    message_id = str(uuid4())
    service.messages.append({"id": message_id, "content": "user question", "status": "queued"})
    runner, objects = runner_for(service)
    client = InterruptedClient(
        [ScriptStep(), ScriptStep(text=RECEIPT)],
        {0: httpx2.RemoteProtocolError("connection dropped")},
        partial_tool=True,
    )
    result = await invoke(runner, client)
    assert result.end_reason == "normal" and result.receipt["accepted"] is True
    assert service.claims == [message_id]
    assert service.deliveries == [{"id": message_id, "claim_token": f"claim-{message_id}"}]
    outgoing = client.received_messages[1]
    assert sum(message.message_id == message_id for message in outgoing) == 1
    assert sum(message.message_id == "bbx-derive-round-2" for message in outgoing) == 1
    assert "POISON" not in str(service.saved)
    assert len(service.beats) == 1
    assert [
        call.kwargs["expected_derive_round"] for call in service.finish_agent.await_args_list
    ] == [2]
    outputs = [
        json.loads(objects.files[call.args[2]["uri"]])["text"]
        for call in service.record_agent_trace.await_args_list
        if call.args[2]["kind"] == "model_output"
    ]
    assert outputs == [RECEIPT]


async def test_completed_tool_result_is_restored_without_executing_old_call(
    monkeypatch, fast_recovery
):
    service = RecoveryService()
    calls = 0

    @tool
    async def step() -> str:
        nonlocal calls
        calls += 1
        return "actual tool output"

    monkeypatch.setattr("bbx_runtime.runner.make_board_tools", lambda _ctx: [step])
    client = InterruptedClient(
        [ScriptStep(calls=(ScriptToolCall("step"),)), ScriptStep(), ScriptStep(text=RECEIPT)],
        {1: httpx2.ReadError("connection dropped")},
    )
    runner, _objects = runner_for(service)
    result = await invoke(runner, client)
    assert result.end_reason == "normal" and calls == 1
    outgoing = client.received_messages[2]
    assert _unpaired_calls(outgoing) == []
    results = [
        content.result
        for message in outgoing
        for content in message.contents
        if content.type == "function_result"
    ]
    assert len(results) == 1 and "actual tool output" in results[0]
    assert len(service.beats) == 2


async def test_recovery_trace_uses_failed_call_step_after_multiple_successes(
    monkeypatch, fast_recovery
):
    service = RecoveryService()

    @tool
    async def step() -> str:
        return "successful lookup"

    monkeypatch.setattr("bbx_runtime.runner.make_board_tools", lambda _ctx: [step])
    client = InterruptedClient(
        [
            ScriptStep(calls=(ScriptToolCall("step"),)),
            ScriptStep(calls=(ScriptToolCall("step"),)),
            ScriptStep(),
            ScriptStep(text=RECEIPT),
        ],
        {2: httpx2.ReadError("connection dropped")},
    )
    runner, _objects = runner_for(service)
    assert (await invoke(runner, client)).end_reason == "normal"
    recovery = [
        call.args[2]
        for call in service.record_agent_trace.await_args_list
        if call.args[2]["kind"] == "board_update"
        and call.args[2]["summary"].startswith("[连接恢复]")
    ]
    failures = [
        call.args[2]
        for call in service.record_agent_trace.await_args_list
        if call.args[2]["kind"] == "model_error"
    ]
    assert len(recovery) == len(failures) == 1
    assert recovery[0]["step"] == failures[0]["step"] == 3


async def test_unknown_tool_result_is_marked_and_never_replayed(monkeypatch, fast_recovery):
    service = RecoveryService()
    session = AgentSession()
    session.state["in_memory"] = {
        "messages": [
            Message(
                role="assistant",
                contents=[Content.from_function_call("old-call", "step", arguments={})],
            )
        ]
    }
    service.saved = {
        "session": session.to_dict(),
        "opening_instructions": "configured",
        "origin": "native",
        "revision": 1,
    }
    calls = 0

    @tool
    async def step() -> str:
        nonlocal calls
        calls += 1
        return "must not run"

    monkeypatch.setattr("bbx_runtime.runner.make_board_tools", lambda _ctx: [step])
    client = InterruptedClient(
        [
            ScriptStep(),
            ScriptStep(
                text='{"accepted":true,"data":{"posted":[],"intent_result":"none","note":"done"}}'
            ),
        ],
        {0: httpx2.ReadError("dropped")},
    )
    runner, _objects = runner_for(service)
    assert (await invoke(runner, client)).end_reason == "normal"
    assert calls == 0 and _unpaired_calls(client.received_messages[1]) == []
    assert any(
        "结果未保存" in str(content.result)
        for message in client.received_messages[1]
        for content in message.contents
        if content.type == "function_result"
    )
    assert service.saved["session"]["session_id"] == session.session_id


async def test_recovery_reinjects_failed_board_delta_and_conclude_once(fast_recovery):
    service = RecoveryService("explore")
    agent = service.board["agents"]["agent-1"]
    agent.update(
        status="concluding",
        conclude_requested_at=datetime.now(UTC).isoformat(),
        conclude_reason="limit",
    )
    service.events_log.append(
        {
            "version": 7,
            "type": "fact.posted",
            "actor": "agent-2",
            "object_id": "F7",
            "payload": {"statement": "fresh evidence"},
        }
    )
    runner, _objects = runner_for(service)
    client = InterruptedClient(
        [ScriptStep(), ScriptStep(text=RECEIPT)], {0: httpx2.ReadError("dropped")}
    )
    assert (await invoke(runner, client, "explore")).end_reason == "normal"
    for messages in client.received_messages:
        text = "\n".join(message.text for message in messages)
        assert text.count("fresh evidence") == 1
        assert text.count("[结束指令]") == 1
    assert len(service.beats) == 1 and service.beats[0]["last_seen_version"] == 7


async def test_success_does_not_reset_per_agent_recovery_allowance(monkeypatch, fast_recovery):
    service = RecoveryService()

    @tool
    async def step() -> str:
        return "new lookup result"

    monkeypatch.setattr("bbx_runtime.runner.make_board_tools", lambda _ctx: [step])
    client = InterruptedClient(
        [
            ScriptStep(),
            ScriptStep(calls=(ScriptToolCall("step"),)),
            ScriptStep(),
            ScriptStep(calls=(ScriptToolCall("step"),)),
            ScriptStep(),
        ],
        {index: httpx2.ReadError("dropped") for index in (0, 2, 4)},
    )
    runner, _objects = runner_for(service)
    result = await invoke(runner, client)
    assert result.end_reason == "runtime_error" and client._index == 5
    assert len(service.beats) == 2 and runner.dispatch_paused("task")
    service.finish_agent.assert_awaited_once()


@pytest.mark.parametrize("status", [400, 401, 402, 403, 409, 429, 500])
async def test_http_model_failures_are_not_transport_recovered(status, fast_recovery):
    service = RecoveryService()
    error = APIStatusError(
        "provider failure",
        response=httpx2.Response(status, request=httpx2.Request("POST", "https://model.invalid")),
        body=None,
    )
    client = InterruptedClient([ScriptStep()], {0: error})
    runner, _objects = runner_for(service)
    assert (await invoke(runner, client)).end_reason == "runtime_error"
    assert client._index == 1 and not runner.dispatch_paused("task")


@pytest.mark.parametrize("phase", ["heartbeat", "session"])
async def test_control_and_session_failures_after_response_do_not_recover(phase, fast_recovery):
    service = RecoveryService()
    if phase == "heartbeat":
        service.heartbeat_error = httpx.ReadError("internal connection dropped")
    else:
        original_heartbeat = service.heartbeat

        async def heartbeat(*args, **kwargs):
            result = await original_heartbeat(*args, **kwargs)
            service.put_error = httpx.ReadError("internal connection dropped")
            return result

        service.heartbeat = heartbeat
    runner, _objects = runner_for(service)
    client = ScriptedChatClient([ScriptStep(text=RECEIPT)])
    if phase == "session":
        with pytest.raises(httpx.ReadError):
            await invoke(runner, client)
    else:
        assert (await invoke(runner, client)).end_reason == "runtime_error"
    assert client._index == 1 and not runner.dispatch_paused("task")
    service.finish_agent.assert_awaited_once()


async def test_waiting_recovery_observes_stop_and_forwards_cancellation(monkeypatch):
    monkeypatch.setattr("bbx_runtime.recovery.CONTROL_POLL_SECONDS", 0.005)
    service = RecoveryService()
    runner, _objects = runner_for(service)
    client = InterruptedClient([ScriptStep()], {0: httpx2.ReadError("dropped")})
    running = asyncio.create_task(invoke(runner, client))
    await asyncio.wait_for(client.failed.wait(), 1)
    await asyncio.sleep(0.01)
    service.board["task"]["status"] = "stopped"
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 1)
    assert client._index == 1
    assert service.finish_agent.call_args.args[-1] == "grace_timeout"


async def test_probe_deadline_cancels_model_read_and_finishes_once(monkeypatch, fast_recovery):
    monkeypatch.setattr("bbx_runtime.recovery.RECOVERY_SECONDS", 0.03)
    service = RecoveryService()
    runner, _objects = runner_for(service)

    class HangingProbe(InterruptedClient):
        def _inner_get_response(self, **kwargs):
            if self._index == 0:
                return super()._inner_get_response(**kwargs)
            self._index += 1

            async def updates():
                await asyncio.Event().wait()
                yield ChatResponseUpdate()

            return self._build_response_stream(updates())

    client = HangingProbe([ScriptStep()], {0: httpx2.ReadError("dropped")})
    result = await asyncio.wait_for(invoke(runner, client), 1)
    assert result.end_reason == "runtime_error" and client._index == 2
    assert result.receipt["error"]["category"] == "connection"
    assert service.beats == []
    service.finish_agent.assert_awaited_once()


async def test_gate_has_one_probe_two_attempts_and_retains_fixed_cooldown(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("bbx_runtime.recovery.monotonic", lambda: now[0])
    gate = ModelRecoveryGate()
    check = AsyncMock()
    original = await gate.acquire(check)
    gate.started(original)
    episode = gate.failed(original, CONNECTION)
    assert episode is not None and episode.deadline == 120 and gate.paused
    now[0] = 2
    first = await gate.acquire(check)
    gate.started(first)
    waiting = asyncio.create_task(gate.acquire(check))
    await asyncio.sleep(0)
    assert not waiting.done() and episode.probes == 1
    gate.failed(first, CONNECTION)
    assert episode.next_probe_at == 6 and episode.deadline == 120
    now[0] = 6
    second = await gate.acquire(check)
    gate.started(second)
    assert episode.probes == 2 and episode.probe_ticket == second.ticket
    gate.failed(second, CONNECTION)
    with pytest.raises(RecoveryExhausted):
        await waiting
    assert gate.paused
    now[0] = 120
    assert not gate.paused
    next_window = await gate.acquire(check)
    gate.started(next_window)
    assert next_window.episode is None
    next_episode = gate.failed(next_window, CONNECTION)
    assert next_episode is not None and next_episode is not episode and next_episode.deadline == 240


async def test_success_releases_waiters_and_late_old_failure_does_not_pause(
    monkeypatch, fast_recovery
):
    gate = ModelRecoveryGate()
    check = AsyncMock()
    first = await gate.acquire(check)
    gate.started(first)
    old_inflight = await gate.acquire(check)
    gate.started(old_inflight)
    gate.failed(first, CONNECTION)
    probe = await gate.acquire(check)
    gate.started(probe)
    waiting = asyncio.create_task(gate.acquire(check))
    await asyncio.sleep(0)
    assert not waiting.done()
    gate.succeeded(probe)
    assert (await waiting).episode is None and not gate.paused
    assert gate.failed(old_inflight, CONNECTION) is None and not gate.paused
    new_request = await gate.acquire(check)
    gate.started(new_request)
    assert gate.failed(new_request, CONNECTION) is not None and gate.paused


async def test_blocked_preflight_is_not_mistaken_for_an_old_model_request():
    service = RecoveryService()
    entered, release = asyncio.Event(), asyncio.Event()
    original_state = service.state

    async def state(_task_id):
        current = asyncio.current_task()
        if current is not None and current.get_name() == "blocked-preflight":
            entered.set()
            await release.wait()
        return await original_state(_task_id)

    service.state = state
    gate = ModelRecoveryGate()
    ctx = RunContext(
        task_id="task",
        agent_id="agent-1",
        task_type="derive",
        state=service.board,
        profile=service.profile,
        service=cast(BlackboardClient, service),
        board=cast(BlackboardClient, service),
        objects=cast(ObjectStore, RecoveryObjects()),
        expected_derive_round=2,
    )
    slow = InterruptedClient([ScriptStep()], {0: httpx2.ReadError("new request failed")})
    healthy = ScriptedChatClient([ScriptStep(text="complete")])
    async with (
        Agent(client=slow, middleware=[BoardSyncMiddleware(ctx, gate)]) as first,
        Agent(client=healthy, middleware=[BoardSyncMiddleware(ctx, gate)]) as second,
    ):
        running = asyncio.create_task(
            first.run("start", stream=True).get_final_response(), name="blocked-preflight"
        )
        await asyncio.wait_for(entered.wait(), 1)
        assert (await second.run("start", stream=True).get_final_response()).text == "complete"
        release.set()
        with pytest.raises(httpx2.ReadError):
            await running
    assert gate.paused and len(slow.received_messages) == 1


async def test_provider_timeout_before_application_deadline_keeps_model_diagnostic(fast_recovery):
    service = RecoveryService()
    wrapped = TimeoutError("provider timeout")
    wrapped.__cause__ = httpx2.ReadTimeout("provider read timed out")
    client = InterruptedClient(
        [ScriptStep(), ScriptStep(), ScriptStep(text=RECEIPT)],
        {0: httpx2.ReadError("dropped"), 1: wrapped},
    )
    runner, objects = runner_for(service)
    assert (await invoke(runner, client)).end_reason == "normal"
    errors = [
        json.loads(json.loads(objects.files[call.args[2]["uri"]])["text"])
        for call in service.record_agent_trace.await_args_list
        if call.args[2]["kind"] == "model_error"
    ]
    assert [error["category"] for error in errors] == ["connection", "timeout"]
    assert errors[1]["transport_type"] == "ReadTimeout" and client._index == 3


async def test_budget_closing_reaches_waiting_explore_and_restricts_next_call(monkeypatch):
    monkeypatch.setattr("bbx_runtime.recovery.RECOVERY_BACKOFF", (0.04, 0.04))
    monkeypatch.setattr("bbx_runtime.recovery.CONTROL_POLL_SECONDS", 0.005)
    service = RecoveryService("explore")
    runner, _objects = runner_for(service)
    receipt = '{"accepted":true,"data":{"posted":[],"intent_result":"none","note":"done"}}'
    client = InterruptedClient(
        [ScriptStep(), ScriptStep(text=receipt)], {0: httpx2.ReadError("dropped")}
    )
    running = asyncio.create_task(invoke(runner, client, "explore"))
    await asyncio.wait_for(client.failed.wait(), 1)
    await asyncio.sleep(0.01)
    service.board["task"]["status"] = "closing"
    service.board["agents"]["agent-1"].update(
        status="concluding",
        conclude_requested_at=datetime.now(UTC).isoformat(),
        conclude_reason="closing",
    )
    assert (await asyncio.wait_for(running, 1)).end_reason == "normal"
    assert "[结束指令] 原因：closing" in "\n".join(
        message.text for message in client.received_messages[1]
    )


@pytest.mark.parametrize("missing_terminal", [False, True])
async def test_native_sdk_sse_recovery_preserves_tool_and_claim_without_partial_replay(
    monkeypatch, fast_recovery, missing_terminal
):
    service = RecoveryService()
    runner, _objects = runner_for(service)
    requests = []
    calls = 0
    message_id = str(uuid4())

    @tool
    async def step() -> str:
        nonlocal calls
        calls += 1
        return "durable native tool output"

    monkeypatch.setattr("bbx_runtime.runner.make_board_tools", lambda _ctx: [step])

    def chunk(delta, finish=None):
        return {
            "id": "chat",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "offline",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def encode(*events):
        return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()

    class DroppedStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield encode(
                chunk({"role": "assistant", "content": "POISON{"}),
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "broken-call",
                                "type": "function",
                                "function": {"name": "step", "arguments": "{"},
                            }
                        ]
                    }
                ),
            )
            if not missing_terminal:
                raise httpx2.RemoteProtocolError("SSE disconnected")

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["stream"] is True
        if len(requests) == 1:
            service.messages.append(
                {"id": message_id, "content": "native user question", "status": "queued"}
            )
            return httpx2.Response(
                200,
                content=encode(
                    chunk(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "native-call",
                                    "type": "function",
                                    "function": {"name": "step", "arguments": "{}"},
                                }
                            ],
                        }
                    ),
                    chunk({}, "tool_calls"),
                ),
                headers={"content-type": "text/event-stream"},
            )
        if len(requests) == 2:
            return httpx2.Response(
                200, stream=DroppedStream(), headers={"content-type": "text/event-stream"}
            )
        assert len(requests) == 3
        assert sum(item.get("tool_call_id") == "native-call" for item in body["messages"]) == 1
        assert any(
            "durable native tool output" in str(item.get("content")) for item in body["messages"]
        )
        assert sum(item.get("content") == "native user question" for item in body["messages"]) == 1
        assert "POISON" not in json.dumps(body)
        return httpx2.Response(
            200,
            content=encode(chunk({"role": "assistant", "content": RECEIPT}), chunk({}, "stop")),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        "bbx_runtime.models.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        ),
    )
    model = service.profile.models.derive.model_copy(
        update={"base_url": "https://offline.invalid/v1"}
    )
    client = make_client(
        model,
        credentials={"api_key": "test-only-key"},
        explore_max_steps=40,
        conclude_grace_calls=3,
        max_duration_seconds=60,
    )
    try:
        assert client.client.max_retries == 4  # type: ignore[attr-defined]
        assert (await invoke(runner, client)).end_reason == "normal"
    finally:
        await close_model_client(client)
    assert calls == 1 and len(requests) == 3 and len(service.beats) == 2
    assert service.claims == [message_id] and len(service.deliveries) == 1
    assert service.deliveries[0]["claim_token"] == f"claim-{message_id}"


async def test_outage_during_preflight_does_not_bypass_reserved_probe(fast_recovery):
    service = RecoveryService()
    entered, release = asyncio.Event(), asyncio.Event()
    original_state = service.state

    async def state(_task_id):
        current = asyncio.current_task()
        if current is not None and current.get_name() == "blocked-preflight":
            entered.set()
            await release.wait()
        return await original_state(_task_id)

    service.state = state
    gate = ModelRecoveryGate()
    ctx = RunContext(
        task_id="task",
        agent_id="agent-1",
        task_type="derive",
        state=service.board,
        profile=service.profile,
        service=cast(BlackboardClient, service),
        board=cast(BlackboardClient, service),
        objects=cast(ObjectStore, RecoveryObjects()),
        expected_derive_round=2,
    )
    blocked = InterruptedClient([ScriptStep()], {0: httpx2.ReadError("new request failed")})
    failing = InterruptedClient([ScriptStep()], {0: httpx2.ReadError("outage")})
    async with (
        Agent(client=blocked, middleware=[BoardSyncMiddleware(ctx, gate)]) as first,
        Agent(client=failing, middleware=[BoardSyncMiddleware(ctx, gate)]) as second,
    ):
        running = asyncio.create_task(
            first.run("start", stream=True).get_final_response(), name="blocked-preflight"
        )
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(httpx2.ReadError):
            await second.run("start", stream=True).get_final_response()
        probe = await gate.acquire(AsyncMock())
        gate.started(probe)
        release.set()
        await asyncio.sleep(0.01)
        assert gate.episode is not None and gate.episode.probe_ticket == probe.ticket
        assert blocked.received_messages == []
        gate.succeeded(probe)
        with pytest.raises(httpx2.ReadError):
            await asyncio.wait_for(running, 1)
    assert gate.paused and len(blocked.received_messages) == 1


async def test_continuous_fast_failures_keep_cooldown_and_reach_original_three_window_limit(
    monkeypatch, fast_recovery
):
    now = [0.0]
    monkeypatch.setattr("bbx_runtime.recovery.monotonic", lambda: now[0])

    class ClockDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(1790726400 + now[0], tz=tz)

    monkeypatch.setattr("bbx_blackboard.domain.rules.datetime", ClockDate)
    state = BoardState(
        task={
            "status": "running",
            "failure_streak": 0,
            "seed_empty_count": 0,
            "params": {"max_consecutive_failures": 3},
        },
        agents={
            f"agent-{index}": {"status": "running", "task_type": "explore"} for index in range(3)
        },
    )
    gate = ModelRecoveryGate()
    for index in range(3):
        now[0] = index * 120
        original = await gate.acquire(AsyncMock())
        gate.started(original)
        episode = gate.failed(original, CONNECTION)
        assert episode is not None and episode.deadline == now[0] + 120
        for _ in range(2):
            probe = await gate.acquire(AsyncMock())
            gate.started(probe)
            gate.failed(probe, CONNECTION)
        with pytest.raises(RecoveryExhausted):
            await gate.acquire(AsyncMock())
        assert gate.paused
        aid = f"agent-{index}"
        events = board_decide(
            state,
            "finish_agent",
            aid,
            {
                "agent_id": aid,
                "end_reason": "runtime_error",
                "receipt": {"accepted": False, "error": CONNECTION},
            },
        )
        payload = events[0]["payload"]
        assert payload["failure_increment"] == 1
        state.task.update(
            failure_streak=state.task["failure_streak"] + payload["failure_increment"],
            failure_window_kind=payload["failure_window_kind"],
            failure_window_started_at=payload["failure_window_started_at"],
        )
        assert [event["type"] for event in events] == (
            ["agent.finished", "task.failed"] if index == 2 else ["agent.finished"]
        )
    assert state.task["failure_streak"] == 3
