"""Real SDK SSE errors retain safe codes through native MAF and member recovery."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from agent_framework.exceptions import ChatClientException
from bbx_runtime.ctf.coordinator import CtfCoordinator, failure_diagnostic
from bbx_runtime.model_errors import is_transient_model_error, model_error_metadata
from bbx_runtime.models import CompleteResponsesClient
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
from openai import APIError, APIStatusError, AsyncOpenAI
from test_ctf_coordination import Runtime
from test_ctf_provider_recovery import RecoveryService
from test_ctf_runner_budget_retry import MEMBER, TURN, runner
from test_runtime_streaming import responses_body, sse


class SseService(RecoveryService):
    def __init__(self):
        super().__init__()
        self.delivered = False

    async def get_profile(self, *_):
        document = await super().get_profile()
        document["profile"]["model"].update(provider="openai_responses", model="offline")
        document["profile"]["model"]["price"]["billing_mode"] = "fixed"
        return document

    async def runtime(self, _, operation, **body):
        if operation == "claim_messages":
            return (
                []
                if self.delivered
                else [
                    {
                        "id": "assignment",
                        "claim_token": "test-claim",
                        "body": "synthetic assignment",
                        "sender_kind": "agent",
                        "sender_id": "lead",
                        "kind": "instruction",
                    }
                ]
            )
        result = await super().runtime(_, operation, **body)
        if operation == "checkpoint" and body["deliveries"]:
            self.delivered = True
        return result


@pytest.mark.parametrize("transport", ["chat", "responses"])
@pytest.mark.parametrize(
    ("code", "category", "transient"),
    [
        ("server_error", "server_error", True),
        ("rate_limit_exceeded", "rate_limit", True),
        ("content_filter", "content_filter", False),
        ("content_policy_violation", "content_filter", False),
        ("invalid_api_key", "authentication", False),
        ("invalid_request_error", "invalid_request", False),
        ("unrecognized_provider_error", "unknown", False),
        ("unsafe code with spaces", "unknown", False),
    ],
)
async def test_http_200_sse_errors_keep_only_safe_structured_fields(
    transport, code, category, transient
):
    requests = []

    def send(request):
        requests.append(request)
        return sse(
            {
                "error": {
                    "code": code,
                    "request_id": "req-safe-123",
                    "message": "private provider body",
                    "headers": {"authorization": "private credential"},
                }
            }
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as http:
        async with AsyncOpenAI(
            api_key="test-only", base_url="https://model.invalid", max_retries=4, http_client=http
        ) as sdk:
            if transport == "responses":
                stream = await sdk.responses.create(
                    model="offline", input="private input", stream=True
                )
            else:
                stream = await sdk.chat.completions.create(
                    model="offline",
                    messages=[{"role": "user", "content": "private input"}],
                    stream=True,
                )
            with pytest.raises(APIError) as caught:
                async for _ in stream:
                    pass
    assert len(requests) == 1  # SDK request retries do not replay an accepted SSE response.
    assert type(caught.value) is APIError
    wrapped = ChatClientException("private wrapper", inner_exception=caught.value, log_level=None)
    metadata = model_error_metadata(wrapped)
    assert metadata["category"] == category
    assert metadata["provider_code"] == (code if " " not in code else None)
    assert metadata["request_id"] == "req-safe-123"
    assert metadata["http_status"] is None
    assert metadata["failure_phase"] == "unknown"
    assert metadata["transient"] is transient
    assert is_transient_model_error(wrapped) is transient
    assert "private" not in json.dumps(metadata)


@pytest.mark.parametrize("nested", [False, True])
def test_api_error_body_fallback_is_bounded_without_reading_message(nested):
    detail = {"code": "rate_limit", "request_id": "req-safe", "message": "private body"}
    body = {"error": detail} if nested else detail
    error = APIError(
        "private exception", request=httpx2.Request("POST", "https://model.invalid"), body=body
    )
    error.code = None
    metadata = model_error_metadata(error)
    assert metadata["provider_code"] == "rate_limit"
    assert metadata["request_id"] == "req-safe"
    assert metadata["transient"] is True
    detail.update(code="private " * 100, request_id="private " * 100)
    metadata = model_error_metadata(error)
    assert metadata["provider_code"] is None
    assert metadata["request_id"] is None
    assert metadata["transient"] is False
    assert "private" not in json.dumps(metadata)


@pytest.mark.parametrize("status", [400, 401, 403])
def test_sse_transient_code_does_not_override_permanent_http_status(status):
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://model.invalid"))
    error = APIStatusError("private", response=response, body={"code": "server_error"})
    assert model_error_metadata(error)["transient"] is False


@pytest.mark.parametrize(
    "code", ["content_filter", "content_policy_violation", "invalid_api_key", "invalid_request"]
)
def test_permanent_provider_code_cannot_be_retried_as_http_server_failure(code):
    response = httpx2.Response(503, request=httpx2.Request("POST", "https://model.invalid"))
    error = APIStatusError("private", response=response, body={"code": code})
    assert model_error_metadata(error)["transient"] is False


@pytest.mark.parametrize(
    "local_error", [TypeError, ValueError, RuntimeError, httpx2.LocalProtocolError]
)
def test_sse_transient_code_does_not_recover_local_failures(local_error):
    error = APIError(
        "private",
        request=httpx2.Request("POST", "https://model.invalid"),
        body={"code": "server_error"},
    )
    error.__cause__ = local_error("private local failure")
    wrapped = ChatClientException("private wrapper", inner_exception=error, log_level=None)
    assert not is_transient_model_error(wrapped)


def function_events(call_id: str, command: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": f"fc-{call_id}",
                "call_id": call_id,
                "name": "execute_command",
                "arguments": "",
                "status": "in_progress",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "sequence_number": 2,
            "output_index": 0,
            "item_id": f"fc-{call_id}",
            "delta": json.dumps({"command": command}),
        },
    ]


@pytest.mark.parametrize("code", ["server_error", "rate_limit_exceeded"])
async def test_native_sse_recovery_preserves_tools_unknown_cost_and_other_member(monkeypatch, code):
    service = SseService()
    requests = []
    executed = []

    async def execute(*args):
        executed.append(args)
        return "confirmed command output"

    def send(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            function = {
                "type": "function_call",
                "id": "fc-confirmed",
                "call_id": "confirmed",
                "name": "execute_command",
                "arguments": json.dumps({"command": "confirmed command"}),
                "status": "completed",
            }
            return sse(
                *function_events("confirmed", "confirmed command"),
                {
                    "type": "response.completed",
                    "sequence_number": 3,
                    "response": responses_body(1, [function]),
                },
            )
        assert len(requests) == 2
        return sse(
            *function_events("partial", "must not execute"),
            {
                "error": {"code": code, "message": "private provider body"},
            },
        )

    coordination = Runtime()
    coordinator = CtfCoordinator(cast(Any, None), cast(Any, None), runner=cast(Any, object()))
    coordinator.service = cast(Any, coordination)
    healthy = asyncio.create_task(asyncio.Event().wait())
    coordinator.active[("task", "lead")] = (healthy, {"id": "healthy", "generation": 1})
    try:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as http:
            async with AsyncOpenAI(
                api_key="test-only",
                base_url="https://model.invalid",
                max_retries=4,
                http_client=http,
            ) as sdk:
                instance = runner(
                    service, CompleteResponsesClient(model="offline", async_client=sdk), monkeypatch
                )
                instance.envd = SimpleNamespace(execute=execute)
                recovered = asyncio.create_task(instance.run("task", MEMBER, TURN))
                coordinator.active[("task", MEMBER["id"])] = (recovered, TURN)
                await recovered
                assert await coordinator.tick("task")
                assert not healthy.done()
                assert ("task", MEMBER["id"]) not in coordinator.active
                assert not any(op == "request_finish" for op, _ in coordination.calls)
        assert len(requests) == 2
        assert len(executed) == 1
        assert service.finished[-1]["end_reason"] == "provider_transient"
        assert service.unknown == {"turn:2": {"turn_id": "turn", "state": "unknown", "cost": "0.6"}}
        assert not service.reservations
        assert service.saved is not None
        session_id = service.saved["session"]["session_id"]
        diagnostic = next(
            row["body"] for row in service.observations if row["kind"] == "model_error"
        )
        assert diagnostic["category"] in {"server_error", "rate_limit"}
        assert diagnostic["provider_code"] == code
        assert diagnostic["exception_type"] == "ChatClientException"
        assert diagnostic["cause_type"] == "APIError"
        resumed = ScriptedChatClient(
            [ScriptStep(text="continue", expect_contains="confirmed command output")]
        )
        instance.client_factory = lambda *_: resumed
        await instance.run("task", MEMBER, {**TURN, "id": "next-turn"})
        assert len(executed) == 1
        assert service.saved["session"]["session_id"] == session_id
        assert service.finished[-1]["end_reason"] == "completed"
        assert len(service.unknown) == 1
    finally:
        await coordinator.close()


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("content_filter", "content_filter"),
        ("content_policy_violation", "content_filter"),
        ("invalid_api_key", "authentication"),
        ("invalid_request", "invalid_request"),
        ("unrecognized_provider_error", "unknown"),
    ],
)
async def test_native_sse_permanent_or_unknown_error_never_replays_partial_tools(
    monkeypatch, code, category
):
    service = SseService()
    requests = []
    executed = []

    def send(request):
        requests.append(request)
        return sse(
            *function_events("partial", "must not execute"),
            {"error": {"code": code, "message": "private provider body"}},
        )

    async def execute(*args):
        executed.append(args)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as http:
        async with AsyncOpenAI(
            api_key="test-only", base_url="https://model.invalid", max_retries=4, http_client=http
        ) as sdk:
            instance = runner(
                service, CompleteResponsesClient(model="offline", async_client=sdk), monkeypatch
            )
            instance.envd = SimpleNamespace(execute=execute)
            with pytest.raises(ChatClientException) as caught:
                await instance.run("task", MEMBER, TURN)
    assert len(requests) == 1
    assert not executed
    assert not service.finished
    assert not service.billed
    assert service.reservations["turn:1"]["state"] == "sent"
    metadata = vars(caught.value)["bbx_model_error"]
    assert metadata["category"] == category
    assert metadata["transient"] is False
    assert "private" not in json.dumps(metadata)


def test_unknown_model_summary_uses_fixed_text_and_content_filter_takes_priority(caplog):
    error = ChatClientException("private provider body", log_level=None)
    vars(error)["bbx_model_error"] = {"category": "unknown", "payload": "private input"}
    diagnostic = failure_diagnostic("turn", error)
    assert "模型调用异常，具体原因未确认" in diagnostic["summary"]
    assert "内部错误" not in diagnostic["summary"]
    vars(error)["bbx_model_error"].update(
        category="content_filter", incomplete_reason="max_output_tokens"
    )
    assert "内容审核" in failure_diagnostic("turn", error)["summary"]
    assert "private" not in json.dumps(diagnostic) + caplog.text
