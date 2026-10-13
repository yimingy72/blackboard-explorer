"""Transient provider recovery preserves confirmed native history and uncertain costs."""

import asyncio
import copy
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from agent_framework import ChatResponseUpdate, Content
from agent_framework.exceptions import (
    ChatClientContentFilterException,
    ChatClientException,
    ChatClientInvalidRequestException,
)
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.execution import DrainPending
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.model_errors import is_transient_model_error, model_error_metadata
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from test_ctf_coordination import Runtime
from test_ctf_runner_budget_retry import MEMBER, TURN, BudgetService, runner


def wrapped_disconnect():
    return ChatClientException(
        "private provider detail", inner_exception=httpx2.RemoteProtocolError("private wire detail")
    )


class RecoveryService(BudgetService):
    def __init__(self):
        super().__init__("sent")
        self.unknown = {}
        self.finished = []
        self.observations = []

    async def runtime(self, _, operation, **body):
        if operation == "record_observation":
            self.observations.append(body)
        if operation == "authorize_member" and "reservation_id" in body:
            self.calls.append(operation)
            self.reservations.setdefault(
                body["reservation_id"],
                {"turn_id": body["turn_id"], "state": "admitted", "cost": "0.6"},
            )
            return {}
        if operation == "finish_turn":
            self.finished.append(copy.deepcopy(body))
            self.unknown.update(
                {
                    key: {**value, "state": "unknown"}
                    for key, value in self.reservations.items()
                    if value["turn_id"] == body["turn_id"] and value["state"] == "sent"
                }
            )
            for key in list(self.reservations):
                if self.reservations[key]["turn_id"] == body["turn_id"]:
                    del self.reservations[key]
        if operation == "checkpoint_failed":
            self.calls.append(operation)
            return {}
        return await super().runtime(_, operation, **body)


class BrokenStream(ScriptedChatClient):
    def __init__(self, error, steps=()):
        super().__init__(steps)
        self.error = error
        self.failed_calls = 0

    def _inner_get_response(self, **kwargs):
        if self._index < len(self.steps):
            return super()._inner_get_response(**kwargs)
        self.failed_calls += 1

        async def updates():
            yield ChatResponseUpdate(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        "partial-call", "execute_command", arguments={"command": "must not execute"}
                    )
                ],
            )
            raise self.error

        return self._build_response_stream(updates())


async def test_wrapped_stream_failure_settles_only_member_and_resumes_saved_tools(monkeypatch):
    service = RecoveryService()
    executed = []

    async def execute(*args):
        executed.append(args)
        return "confirmed command output"

    broken = BrokenStream(
        wrapped_disconnect(),
        [ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "confirmed command"}),))],
    )
    instance = runner(service, broken, monkeypatch)
    instance.envd = SimpleNamespace(execute=execute)
    await instance.run("task", MEMBER, TURN)
    assert broken.failed_calls == 1
    assert len(executed) == 1
    assert service.finished[-1]["end_reason"] == "provider_transient"
    assert service.unknown == {"turn:2": {"turn_id": "turn", "state": "unknown", "cost": "0.6"}}
    assert not service.reservations
    assert service.saved is not None
    saved_session_id = service.saved["session"]["session_id"]
    diagnostic = next(row["body"] for row in service.observations if row["kind"] == "model_error")
    assert diagnostic["transient"] is True
    assert diagnostic["attempt_limit"] == 5
    assert diagnostic["transport_type"] == "RemoteProtocolError"
    resumed = ScriptedChatClient(
        [ScriptStep(text="continue assignment", expect_contains="confirmed command output")]
    )
    instance.client_factory = lambda *_: resumed
    await instance.run("task", MEMBER, {**TURN, "id": "next-turn"})
    assert len(executed) == 1
    assert service.saved["session"]["session_id"] == saved_session_id
    assert service.finished[-1]["end_reason"] == "completed"
    assert len(service.unknown) == 1
    assert len(service.billed) == 2


@pytest.mark.parametrize(
    "error",
    [
        ChatClientContentFilterException("filtered"),
        ChatClientInvalidRequestException("invalid"),
        ChatClientException("local protocol", inner_exception=httpx2.LocalProtocolError("invalid")),
        ChatClientException("local logic", inner_exception=TypeError("invalid")),
        ChatClientException("local logic", inner_exception=RuntimeError("invalid")),
    ],
)
async def test_permanent_and_internal_provider_errors_never_recover(monkeypatch, error):
    service = RecoveryService()
    client = BrokenStream(error)
    with pytest.raises(type(error)):
        await runner(service, client, monkeypatch).run("task", MEMBER, TURN)
    assert client.failed_calls == 1
    assert not service.finished
    assert not is_transient_model_error(error)


def test_internal_error_with_transport_context_is_not_provider_failure():
    error = RuntimeError("internal error")
    error.__context__ = httpx2.RemoteProtocolError("unrelated transport error")
    assert not is_transient_model_error(error)
    local = APIConnectionError(request=httpx2.Request("POST", "https://model.invalid"))
    local.__cause__ = httpx2.LocalProtocolError("request is invalid")
    assert model_error_metadata(local)["transient"] is False


@pytest.mark.parametrize("status", [408, 429, 503])
async def test_transient_status_error_ends_turn_for_durable_recovery(monkeypatch, status):
    response = httpx2.Response(
        status, request=httpx2.Request("POST", "https://model.invalid"), json={"error": {}}
    )
    error = ChatClientException(
        "provider unavailable",
        inner_exception=APIStatusError("offline", response=response, body={}),
    )
    service = RecoveryService()
    await runner(service, BrokenStream(error), monkeypatch).run("task", MEMBER, TURN)
    assert service.finished[-1]["end_reason"] == "provider_transient"


async def test_review_failure_does_not_reopen_task(monkeypatch):
    service = RecoveryService()
    error = APITimeoutError(request=httpx2.Request("POST", "https://model.invalid"))
    with pytest.raises(APITimeoutError):
        await runner(service, BrokenStream(error), monkeypatch).run(
            "task", MEMBER, {**TURN, "purpose": "review"}
        )
    assert not service.finished


async def test_provider_recovery_waits_for_remote_member_drain(monkeypatch):
    class DrainService(RecoveryService):
        async def runtime_state(self, _):
            return {**await super().runtime_state(_), "members": [MEMBER]}

    pending = True

    async def stop_member(*_):
        if pending:
            raise DrainPending()
        return {}

    service = DrainService()
    instance = runner(service, BrokenStream(wrapped_disconnect()), monkeypatch)
    instance.envd = SimpleNamespace(stop_member=stop_member)
    await instance.run("task", MEMBER, TURN)
    assert not service.finished
    assert instance.pending_settlements[("task", MEMBER["id"])][2] == "provider_transient"
    pending = False
    await instance.retry_settlements("task")
    assert service.finished[-1]["end_reason"] == "provider_transient"
    assert not instance.pending_settlements


async def test_checkpoint_failure_blocks_provider_recovery(monkeypatch):
    class FailedCheckpointService(RecoveryService):
        async def runtime(self, _, operation, **body):
            if operation == "checkpoint":
                raise RuntimeError("checkpoint failed")
            return await super().runtime(_, operation, **body)

    service = FailedCheckpointService()
    instance = runner(service, BrokenStream(wrapped_disconnect()), monkeypatch)
    with pytest.raises(RuntimeError, match="checkpoint failed"):
        await instance.run("task", MEMBER, TURN)
    assert ("task", "member-1") in instance.pending_checkpoints
    assert not service.finished


async def test_member_recovery_completion_preserves_other_active_turns():
    service = Runtime()
    instance = CtfCoordinator(cast(Any, None), cast(Any, None), runner=cast(CtfRunner, object()))
    instance.service = cast(Any, service)
    healthy = asyncio.create_task(asyncio.Event().wait())
    recovered = asyncio.create_task(asyncio.sleep(0))
    await recovered
    instance.active[("task", "member-1")] = (recovered, {"id": "settled-provider-turn"})
    instance.active[("task", "lead")] = (healthy, {"id": "healthy", "generation": 1})
    try:
        assert await instance.tick("task")
        assert not healthy.done()
        assert ("task", "member-1") not in instance.active
        assert not any(operation == "request_finish" for operation, _ in service.calls)
    finally:
        await instance.close()


async def test_sdk_attempt_limit_does_not_retry_response_stream_iteration():
    requests = []

    class LostStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": [{"index": 0, "delta": {"content": "partial"}}]}\n\n'
            raise httpx2.RemoteProtocolError("simulated response stream loss")

    def send(request):
        requests.append(request)
        return httpx2.Response(200, stream=LostStream())

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as http:
        async with AsyncOpenAI(
            api_key="test-only-key",
            base_url="https://model.invalid",
            max_retries=4,
            http_client=http,
        ) as client:
            stream = await client.chat.completions.create(
                model="offline", messages=[{"role": "user", "content": "offline"}], stream=True
            )
            with pytest.raises(APIConnectionError) as caught:
                async for _ in stream:
                    pass
            assert isinstance(caught.value.__cause__, httpx2.RemoteProtocolError)
    assert len(requests) == 1
