"""Pi's real HTTP adapters over an injected, network-free fetch transport."""

import json
from pathlib import Path

import httpx2
import pytest
from bbx_runtime.model_errors import ModelStreamError, model_error_metadata
from bbx_runtime.models import load_runtime_profile, make_client, model_run_options
from bbx_runtime.pi import Message


def client(handler, *, retries=0):
    model = load_runtime_profile(
        Path(__file__).resolve().parents[4] / "profiles/default"
    ).models.explore
    instance = make_client(
        model,
        api_key="test-only",
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=20,
        max_retries=retries,
    )
    instance.request_transport = httpx2.MockTransport(handler)
    return instance, model


def completed(*, usage=True):
    event = {
        "id": "offline",
        "model": "deepseek-flash",
        "choices": [
            {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"},
        ],
    }
    if usage:
        event["usage"] = {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "prompt_cache_hit_tokens": 8,
            "completion_tokens_details": {"reasoning_tokens": 1},
        }
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode(),
    )


async def test_pi_preserves_cache_usage_and_native_max_effort():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return completed()

    instance, model = client(respond)
    try:
        options = model_run_options(model.model_copy(update={"reasoning_effort": "max"}))
        response = await instance.get_response([Message("user", ["one word"])], options=options)
        assert response.text == "ok"
        assert response.usage_details["input_token_count"] == 12
        assert response.usage_details["prompt_cache_hit_tokens"] == 8
        assert response.usage_details["prompt_cache_miss_tokens"] == 4
        assert response.usage_details["reasoning_output_token_count"] == 1
        assert requests[0]["stream"] is True
        assert requests[0]["reasoning_effort"] == "max"
        assert requests[0]["max_tokens"] == 393216
    finally:
        await instance.close()


async def test_missing_provider_usage_is_never_reported_as_known_zero():
    instance, _ = client(lambda _: completed(usage=False))
    try:
        response = await instance.get_response([Message("user", ["one word"])])
        assert response.usage_details is None
    finally:
        await instance.close()


async def test_native_sdk_retries_429_once_then_completes():
    requests = []

    def respond(request):
        requests.append(request)
        return (
            completed()
            if len(requests) == 2
            else httpx2.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": {"code": "rate_limit"}},
            )
        )

    instance, _ = client(respond, retries=1)
    try:
        assert (await instance.get_response([Message("user", ["one word"])])).text == "ok"
        assert len(requests) == 2
    finally:
        await instance.close()


@pytest.mark.parametrize(
    "status,category",
    [
        (400, "invalid_request"),
        (401, "authentication"),
        (402, "payment_required"),
        (403, "authentication"),
        (429, "rate_limit"),
        (503, "server_error"),
    ],
)
async def test_pi_failures_keep_safe_status_and_never_provider_body(status, category):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(
            status,
            headers={"x-request-id": "req-safe", "retry-after": "1"},
            json={"error": {"message": "synthetic private provider payload"}},
        )

    instance, _ = client(respond)
    try:
        with pytest.raises(ModelStreamError) as failure:
            await instance.get_response([Message("user", ["synthetic private input"])])
        metadata = model_error_metadata(failure.value)
        assert metadata["category"] == category
        assert metadata["http_status"] == status
        assert metadata["request_id"] == "req-safe"
        assert len(calls) == 1
        assert "private" not in json.dumps(metadata)
    finally:
        await instance.close()
