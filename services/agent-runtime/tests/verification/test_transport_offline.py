"""OpenAI-compatible transport failures and DeepSeek-shaped usage, without network."""

import json

import httpx2 as httpx
import pytest
from agent_framework import Message
from agent_framework.exceptions import ChatClientException
from agent_framework.openai import OpenAIChatCompletionClient
from bbx_runtime.models import DeepSeekChatClient, DeepSeekChatOptions
from openai import AsyncOpenAI


def _completion() -> dict:
    return {
        "id": "fake-completion",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
            "prompt_cache_hit_tokens": 7,
            "prompt_cache_miss_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    }


def _client(handler, client_type: type[OpenAIChatCompletionClient] = DeepSeekChatClient):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    sdk = AsyncOpenAI(
        api_key="test-only-key",
        base_url="https://model.invalid/v1",
        http_client=http,
        max_retries=2,
    )
    return client_type(model="deepseek-flash", async_client=sdk), http


async def test_02_maf_drops_deepseek_specific_cache_usage_fields():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion())

    client, http = _client(respond, OpenAIChatCompletionClient)
    async with http:
        response = await client.get_response([Message(role="user", contents=["one word"])])
    assert response.text == "ok"
    assert response.usage_details is not None
    assert response.usage_details.get("input_token_count") == 12
    assert response.usage_details.get("reasoning_output_token_count") == 1
    assert response.usage_details.get("prompt_cache_hit_tokens") is None
    assert response.usage_details.get("prompt_cache_miss_tokens") is None
    assert response.raw_representation is None


async def test_02_deepseek_adapter_keeps_standard_and_cache_usage_fields():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion())

    client, http = _client(respond)
    async with http:
        response = await client.get_response([Message(role="user", contents=["one word"])])
    assert response.usage_details is not None
    assert response.usage_details.get("input_token_count") == 12
    assert response.usage_details.get("output_token_count") == 3
    assert response.usage_details.get("reasoning_output_token_count") == 1
    assert response.usage_details.get("prompt_cache_hit_tokens") == 7
    assert response.usage_details.get("prompt_cache_miss_tokens") == 5
    assert response.usage_details.get("cache_read_input_token_count") == 7
    assert response.raw_representation is None


async def test_reasoning_effort_reaches_chat_completions_request():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion())

    client, http = _client(respond)
    options: DeepSeekChatOptions = {"reasoning_effort": "high", "max_tokens": 64}
    async with http:
        await client.get_response([Message(role="user", contents=["one word"])], options=options)
    assert captured.get("reasoning_effort") == "high"
    assert captured.get("max_completion_tokens") == 64


async def test_10_sdk_retries_injected_429_then_succeeds():
    attempts = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(
                429,
                json={"error": {"message": "simulated limit", "type": "rate_limit_error"}},
                headers={"retry-after-ms": "1"},
            )
        return httpx.Response(200, json=_completion())

    client, http = _client(respond)
    async with http:
        response = await client.get_response([Message(role="user", contents=["one word"])])
    assert response.text == "ok"
    assert attempts == 3


async def test_10_sdk_retries_injected_timeout_then_surfaces_failure():
    attempts = 0

    def timeout(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("simulated timeout")

    client, http = _client(timeout)
    async with http:
        with pytest.raises(ChatClientException):
            await client.get_response([Message(role="user", contents=["one word"])])
    assert attempts == 3
