"""Offline model failure diagnostics never contain provider or prompt text."""

import json
from typing import Any, cast

import httpx2
import pytest
from agent_framework import Content, Message
from agent_framework.exceptions import ChatClientException
from bbx_runtime.model_errors import model_attempt_limit, model_error_metadata
from bbx_runtime.models import make_client
from openai import APIConnectionError, APIStatusError, APITimeoutError


def status_error(status: int, code: str = "safe_code", **headers: str) -> APIStatusError:
    response = httpx2.Response(
        status,
        request=httpx2.Request("POST", "https://model.invalid/v1/chat/completions"),
        headers=headers,
        json={"error": {"code": code, "message": "secret provider body"}},
    )
    return APIStatusError("secret provider message", response=response, body=response.json())


@pytest.mark.parametrize(
    ("status", "code", "category", "transient"),
    [
        (429, "rate_limit", "rate_limit", True),
        (503, "server_error", "server_error", True),
        (400, "bad_request", "invalid_request", False),
        (401, "bad_auth", "authentication", False),
        (403, "forbidden", "authentication", False),
        (402, "insufficient_balance", "payment_required", False),
        (400, "content_filter", "content_filter", False),
        (408, "timeout", "timeout", True),
        (409, "conflict", "conflict", True),
    ],
)
def test_status_error_category_and_safe_metadata(status, code, category, transient):
    error = status_error(status, code, **{"x-request-id": "req-123", "retry-after": "2"})
    wrapped = ChatClientException("secret wrapper", inner_exception=error)
    metadata = model_error_metadata(
        wrapped,
        elapsed_ms=125,
        messages=[Message(role="user", contents=[Content.from_text("secret prompt")])],
        attempt_limit=5,
    )
    assert metadata == {
        "category": category,
        "transient": transient,
        "http_status": status,
        "provider_code": code,
        "request_id": "req-123",
        "retry_after": "2",
        "elapsed_ms": 125,
        "message_count": 1,
        "text_chars": 13,
        "attempt_limit": 5,
    }
    assert "secret" not in json.dumps(metadata)


@pytest.mark.parametrize(
    ("error", "category", "transient"),
    [
        (
            APIConnectionError(request=httpx2.Request("POST", "https://model.invalid")),
            "connection",
            True,
        ),
        (APITimeoutError(request=httpx2.Request("POST", "https://model.invalid")), "timeout", True),
        (RuntimeError("secret unknown error"), "unknown", False),
    ],
)
def test_non_http_categories(error, category, transient):
    metadata = model_error_metadata(error)
    assert metadata["category"] == category
    assert metadata["transient"] is transient


def test_nested_causes_and_untrusted_headers_are_bounded():
    error = status_error(
        429,
        "secret prompt with spaces",
        **{"x-request-id": "secret request id", "retry-after": "secret retry value"},
    )
    outer = RuntimeError("secret outer")
    outer.__cause__ = ChatClientException("secret middle", inner_exception=error)
    metadata = model_error_metadata(outer, elapsed_ms=10**20)
    assert metadata["category"] == "rate_limit"
    assert metadata["provider_code"] is None
    assert metadata["request_id"] is None
    assert metadata["retry_after"] is None
    assert metadata["elapsed_ms"] == 86_400_000
    assert "secret" not in json.dumps(metadata)


@pytest.mark.parametrize(
    "provider",
    [
        "deepseek",
        "openai_chat",
        "openai_responses",
        "openai_compatible",
        "azure_openai_chat",
        "azure_openai_responses",
    ],
)
async def test_openai_sdk_retries_are_configured_without_application_loop(provider):
    from pathlib import Path

    from bbx_runtime.models import close_model_client, load_runtime_profile

    profile_dir = Path(__file__).resolve().parents[3] / "profiles" / "default"
    model = load_runtime_profile(profile_dir).models.explore.model_copy(
        update={
            "provider": provider,
            "base_url": "https://model.invalid/v1",
            "provider_options": {"api_version": "2025-01-01-preview"},
        }
    )
    client = cast(
        Any,
        make_client(
            model,
            api_key="test-only-key",
            explore_max_steps=1,
            conclude_grace_calls=0,
            max_duration_seconds=10,
        ),
    )
    assert client.client.max_retries == 4
    assert model_attempt_limit(provider) == 5
    await close_model_client(client)


def test_non_openai_provider_reports_one_attempt() -> None:
    assert model_attempt_limit("anthropic") == 1
