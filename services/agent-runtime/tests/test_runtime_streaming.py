"""OpenAI-compatible SSE is consumed by MAF before tools or receipts are accepted."""

import json
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from agent_framework import Agent, ChatContext, ChatMiddleware, ChatResponse, ResponseStream
from bbx_runtime.model_errors import ModelStreamError, model_error_metadata
from bbx_runtime.models import (
    close_model_client,
    load_runtime_profile,
    make_client,
    model_run_options,
)
from bbx_runtime.trace import model_content
from openai import AsyncOpenAI

PROFILE_DIR = Path(__file__).resolve().parents[3] / "profiles/default"


def sse(*events: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        text="".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def chat_chunk(index: int, delta: dict[str, Any], finish: str | None = None, usage=None):
    return {
        "id": f"chat-{index}",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
        if delta or finish
        else [],
        "usage": usage,
    }


@pytest.mark.parametrize("provider", ["deepseek", "openai_chat", "openai_compatible"])
async def test_chat_sse_tool_loop_usage_reasoning_and_history(monkeypatch, provider):
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        if len(requests) == 1:
            return sse(
                chat_chunk(1, {"role": "assistant", "reasoning_content": "part-A"}),
                chat_chunk(
                    1,
                    {
                        "reasoning_content": "part-B",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"name":"'},
                            }
                        ],
                    },
                ),
                chat_chunk(
                    1,
                    {"tool_calls": [{"index": 0, "function": {"arguments": 'a"}'}}]},
                ),
                chat_chunk(1, {}, "tool_calls"),
                chat_chunk(
                    1,
                    {},
                    usage={
                        "prompt_tokens": 6,
                        "completion_tokens": 3,
                        "total_tokens": 9,
                        "prompt_cache_hit_tokens": 2,
                        "prompt_cache_miss_tokens": 4,
                    },
                ),
            )
        assert body["messages"][-1]["content"] == "found a"
        return sse(
            chat_chunk(2, {"role": "assistant", "content": "found "}),
            chat_chunk(2, {"content": "a"}),
            chat_chunk(2, {}, "stop"),
            chat_chunk(
                2,
                {},
                usage={
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                    "prompt_cache_hit_tokens": 3,
                    "prompt_cache_miss_tokens": 5,
                },
            ),
        )

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "bbx_runtime.models.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport)),
    )
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={"provider": provider, "model": "test-model", "base_url": "https://model.test/v1"}
    )
    client = make_client(
        model,
        credentials={"api_key": "test-only-key"},
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=10,
    )
    calls: list[str] = []
    responses: list[ChatResponse] = []

    class Capture(ChatMiddleware):
        async def process(self, context: ChatContext, call_next) -> None:
            await call_next()
            if isinstance(context.result, ResponseStream):
                context.result.with_result_hook(lambda response: responses.append(response))

    def lookup(name: str) -> str:
        calls.append(name)
        return f"found {name}"

    try:
        async with Agent(client=client, tools=[lookup], middleware=[Capture()]) as agent:
            stream = agent.run("Find a", stream=True, options=model_run_options(model))
            result = await stream.get_final_response()
        assert result.text == "found a"
        assert calls == ["a"]
        assert len(requests) == 2
        assert result.usage_details is not None
        assert result.usage_details.get("input_token_count") == 14
        assert result.usage_details.get("output_token_count") == 5
        if provider == "deepseek":
            assert model_content(responses[0])[1] == "part-Apart-B"
    finally:
        await close_model_client(client)


@pytest.mark.parametrize("terminal", ["missing", "length"])
async def test_chat_sse_partial_reply_is_rejected(monkeypatch, terminal):
    def respond(_request: httpx.Request) -> httpx.Response:
        events = [chat_chunk(1, {"role": "assistant", "content": '{"accepted":true}'})]
        if terminal == "length":
            events.append(chat_chunk(1, {}, "length"))
        return sse(*events)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "bbx_runtime.models.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport)),
    )
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": "openai_chat",
            "model": "test-model",
            "base_url": "https://model.test/v1",
        }
    )
    client = make_client(
        model,
        credentials={"api_key": "test-only-key"},
        explore_max_steps=1,
        conclude_grace_calls=0,
        max_duration_seconds=10,
    )
    try:
        async with Agent(client=client) as agent:
            with pytest.raises((httpx.RemoteProtocolError, ModelStreamError)) as failure:
                await agent.run("Start", stream=True).get_final_response()
        category = "connection" if terminal == "missing" else "invalid_response"
        assert model_error_metadata(failure.value)["category"] == category
    finally:
        await close_model_client(client)


def responses_body(index: int, output: list[dict[str, Any]], status: str = "completed"):
    return {
        "id": f"resp_{index}",
        "object": "response",
        "created_at": 1,
        "model": "test-model",
        "status": status,
        "output": output,
        "usage": {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }


async def test_responses_sse_replays_local_function_history(monkeypatch):
    requests: list[dict[str, Any]] = []
    function = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call-1",
        "name": "lookup",
        "arguments": '{"name":"a"}',
        "status": "completed",
    }
    message = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "found a", "annotations": []}],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body["stream"] is True and body["store"] is False
        if len(requests) == 1:
            return sse(
                {
                    "type": "response.output_item.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item": {**function, "arguments": "", "status": "in_progress"},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "fc_1",
                    "delta": '{"name":"',
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item_id": "fc_1",
                    "delta": 'a"}',
                },
                {
                    "type": "response.completed",
                    "sequence_number": 4,
                    "response": responses_body(1, [function]),
                },
            )
        assert any(
            item.get("type") == "function_call_output"
            and item["call_id"] == "call-1"
            and item["output"] == "found a"
            for item in body["input"]
        )
        return sse(
            {
                "type": "response.content_part.added",
                "sequence_number": 1,
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg_1",
                "part": {"type": "output_text", "text": None, "annotations": []},
            },
            {
                "type": "response.output_text.delta",
                "sequence_number": 2,
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg_1",
                "delta": "found ",
            },
            {
                "type": "response.output_text.delta",
                "sequence_number": 3,
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg_1",
                "delta": "a",
            },
            {
                "type": "response.completed",
                "sequence_number": 4,
                "response": responses_body(2, [message]),
            },
        )

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "bbx_runtime.models.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport)),
    )
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": "openai_responses",
            "model": "test-model",
            "base_url": "https://model.test/v1",
        }
    )
    client = make_client(
        model,
        credentials={"api_key": "test-only-key"},
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=10,
    )
    calls: list[str] = []

    def lookup(name: str) -> str:
        calls.append(name)
        return f"found {name}"

    try:
        async with Agent(client=client, tools=[lookup]) as agent:
            result = await agent.run(
                "Find a", stream=True, options=model_run_options(model)
            ).get_final_response()
        assert result.text == "found a"
        assert calls == ["a"]
        assert len(requests) == 2
        assert result.usage_details is not None
        assert result.usage_details.get("input_token_count") == 14
    finally:
        await close_model_client(client)


@pytest.mark.parametrize("status", ["failed", "incomplete", "missing"])
async def test_responses_sse_partial_reply_is_rejected(monkeypatch, status):
    message = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": '{"accepted":true}', "annotations": []}],
    }

    def respond(_request: httpx.Request) -> httpx.Response:
        events = [
            {
                "type": "response.output_text.delta",
                "sequence_number": 1,
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg_1",
                "delta": '{"accepted":true}',
            }
        ]
        if status != "missing":
            events.append(
                {
                    "type": f"response.{status}",
                    "sequence_number": 2,
                    "response": responses_body(1, [message], status),
                }
            )
        return sse(*events)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "bbx_runtime.models.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport)),
    )
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": "openai_responses",
            "model": "test-model",
            "base_url": "https://model.test/v1",
        }
    )
    client = make_client(
        model,
        credentials={"api_key": "test-only-key"},
        explore_max_steps=1,
        conclude_grace_calls=0,
        max_duration_seconds=10,
    )
    try:
        async with Agent(client=client) as agent:
            with pytest.raises((httpx.RemoteProtocolError, ModelStreamError)) as failure:
                await agent.run(
                    "Start", stream=True, options=model_run_options(model)
                ).get_final_response()
        assert model_error_metadata(failure.value)["category"] == (
            "connection"
            if status == "missing"
            else "invalid_response"
            if status == "incomplete"
            else "unknown"
        )
    finally:
        await close_model_client(client)


@pytest.mark.parametrize(
    ("event_type", "code", "category"),
    [
        ("response.failed", "rate_limit_exceeded", "rate_limit"),
        ("response.failed", "server_error", "server_error"),
        ("error", "rate_limit", "rate_limit"),
        ("error", "unexpected_code", "unknown"),
    ],
)
async def test_responses_sse_error_codes_are_classified(monkeypatch, event_type, code, category):
    def respond(_request: httpx.Request) -> httpx.Response:
        event = (
            {"type": "error", "sequence_number": 1, "code": code, "message": "private body"}
            if event_type == "error"
            else {
                "type": "response.failed",
                "sequence_number": 1,
                "response": {
                    **responses_body(1, [], "failed"),
                    "error": {"code": code, "message": "private body"},
                },
            }
        )
        return sse(event)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "bbx_runtime.models.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport)),
    )
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": "openai_responses",
            "model": "test-model",
            "base_url": "https://model.test/v1",
        }
    )
    client = make_client(
        model,
        credentials={"api_key": "test-only-key"},
        explore_max_steps=1,
        conclude_grace_calls=0,
        max_duration_seconds=10,
    )
    try:
        async with Agent(client=client) as agent:
            with pytest.raises(ModelStreamError) as failure:
                await agent.run(
                    "Start", stream=True, options=model_run_options(model)
                ).get_final_response()
        metadata = model_error_metadata(failure.value)
        assert metadata["category"] == category
        assert "private body" not in str(metadata)
    finally:
        await close_model_client(client)
