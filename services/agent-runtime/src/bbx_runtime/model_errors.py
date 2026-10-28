"""Bounded, content-free diagnostics for failed model requests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any

import httpx2
from agent_framework import Message
from agent_framework.exceptions import (
    ChatClientContentFilterException,
    ChatClientException,
    ChatClientInvalidAuthException,
    ChatClientInvalidRequestException,
)
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    ContentFilterFinishReasonError,
)

OPENAI_PROVIDERS = {
    "deepseek",
    "openai_chat",
    "openai_responses",
    "openai_compatible",
    "azure_openai_chat",
    "azure_openai_responses",
}
PROVIDER_ERROR_CATEGORIES = {
    "authentication",
    "connection",
    "conflict",
    "payment_required",
    "rate_limit",
    "server_error",
    "timeout",
}


class ModelStreamError(RuntimeError):
    """A provider terminal event that cannot be used as a complete model turn."""

    def __init__(
        self,
        category: str,
        *,
        event_type: str | None = None,
        provider_code: str | None = None,
        incomplete_reason: str | None = None,
        finish_reason: str | None = None,
        provider_response_id: str | None = None,
        terminal_usage: Mapping[str, Any] | None = None,
    ) -> None:
        self.category = category
        self.event_type = (
            event_type if isinstance(event_type, str) and event_type in _STREAM_EVENTS else None
        )
        self.provider_code = _bounded_identifier(provider_code, _IDENTIFIER)
        self.incomplete_reason = _bounded_identifier(incomplete_reason, _IDENTIFIER)
        self.finish_reason = finish_reason if finish_reason in _FINISH_REASONS else None
        self.provider_response_id = _bounded_identifier(provider_response_id, _REQUEST_ID)
        self.terminal_usage = validated_terminal_usage(terminal_usage)
        super().__init__("Model stream did not complete successfully")


class IncompleteModelStreamError(httpx2.RemoteProtocolError):
    """A locally detected stream EOF without a valid completion event."""


class ModelRequestValidationError(ChatClientInvalidRequestException):
    """A request rejected by the local adapter before any provider HTTP call."""


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SECONDS = re.compile(r"[0-9]{1,8}(?:\.[0-9]{1,3})?\Z")
_STREAM_EVENTS = {"response.failed", "response.incomplete", "error"}
_FINISH_REASONS = {"stop", "tool_calls", "length", "function_call", "content_filter"}
_TRANSPORT_TYPES = (
    httpx2.ProxyError,
    httpx2.ConnectError,
    httpx2.ReadError,
    httpx2.RemoteProtocolError,
    httpx2.LocalProtocolError,
    httpx2.ConnectTimeout,
    httpx2.ReadTimeout,
    httpx2.WriteError,
    httpx2.WriteTimeout,
    httpx2.PoolTimeout,
)
_EXCEPTION_TYPES = {
    IncompleteModelStreamError,
    ModelRequestValidationError,
    ModelStreamError,
    APIError,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    ChatClientException,
    ChatClientContentFilterException,
    ChatClientInvalidAuthException,
    ChatClientInvalidRequestException,
    ContentFilterFinishReasonError,
    RuntimeError,
    ValueError,
    TypeError,
    *_TRANSPORT_TYPES,
}


def _bounded_identifier(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def validated_terminal_usage(details: Mapping[str, Any] | None) -> dict[str, int] | None:
    """Accept complete, consistent terminal token counts without retaining raw provider data."""
    if not details:
        return None
    required = ("input_token_count", "output_token_count", "total_token_count")
    optional = ("cache_read_input_token_count", "reasoning_output_token_count")
    if any(details.get(key) is None for key in required):
        return None
    counts = {key: details.get(key, 0) for key in (*required, *optional)}
    if any(type(value) is not int or not 0 <= value <= 1_000_000_000 for value in counts.values()):
        return None
    if (
        counts["total_token_count"] != counts["input_token_count"] + counts["output_token_count"]
        or counts["cache_read_input_token_count"] > counts["input_token_count"]
        or counts["reasoning_output_token_count"] > counts["output_token_count"]
    ):
        return None
    return counts


def terminal_model_error(error: BaseException) -> ModelStreamError | None:
    """Unwrap only our rejected terminal response, never infer usage from transport failures."""
    pending = [error]
    seen: set[int] = set()
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ModelStreamError):
            return current
        if isinstance(current, ChatClientException):
            pending.extend(
                item
                for item in (current.__cause__, *current.args)
                if isinstance(item, BaseException)
            )
    return None


def _safe_exception_type(error: BaseException) -> str | None:
    return type(error).__name__ if type(error) in _EXCEPTION_TYPES else None


def _retry_after(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    if _SECONDS.fullmatch(value):
        return value
    try:
        date = parsedate_to_datetime(value)
        return format_datetime(date.astimezone(UTC), usegmt=True) if date.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _exceptions(error: BaseException) -> list[BaseException]:
    """MAF wraps provider errors in causes, contexts, and exception args."""
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    found: list[BaseException] = []
    while pending and len(found) < 16:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        pending.extend(
            child
            for child in (current.__cause__, current.__context__, *current.args)
            if isinstance(child, BaseException)
        )
    return found


def model_error_metadata(
    error: BaseException,
    *,
    elapsed_ms: int = 0,
    messages: Sequence[Message] = (),
    attempt_limit: int = 1,
) -> dict[str, str | int | bool | None]:
    """Return only fixed categories, validated provider IDs, and bounded counts."""
    chain = _exceptions(error)
    status_error = next((item for item in chain if isinstance(item, APIStatusError)), None)
    status = status_error.status_code if status_error is not None else None
    status = status if type(status) is int and 100 <= status <= 599 else None
    code = None
    request_id = None
    retry_after = None
    # HTTP 200 SSE errors are plain APIError, with no status or response headers.
    api_error = status_error or next((item for item in chain if isinstance(item, APIError)), None)
    if api_error is not None:
        code = _bounded_identifier(api_error.code, _IDENTIFIER)
        request_id = _bounded_identifier(getattr(api_error, "request_id", None), _REQUEST_ID)
        body = api_error.body
        if isinstance(body, dict):
            detail = body.get("error")
            detail = detail if isinstance(detail, dict) else body
            code = code or _bounded_identifier(detail.get("code"), _IDENTIFIER)
            request_id = (
                request_id
                or _bounded_identifier(detail.get("request_id"), _REQUEST_ID)
                or _bounded_identifier(body.get("request_id"), _REQUEST_ID)
            )
    if status_error is not None:
        retry_after = _retry_after(status_error.response.headers.get("retry-after"))

    stream_error = next((item for item in chain if isinstance(item, ModelStreamError)), None)
    transport_error = next((item for item in chain if isinstance(item, _TRANSPORT_TYPES)), None)
    transport_type = (
        next(
            (kind.__name__ for kind in _TRANSPORT_TYPES if isinstance(transport_error, kind)),
            None,
        )
        if transport_error is not None
        else None
    )
    if any(isinstance(item, ModelRequestValidationError) for item in chain):
        failure_phase = "request_validation"
    elif status is not None:
        failure_phase = "http_response"
    elif any(isinstance(item, IncompleteModelStreamError) for item in chain) or stream_error:
        failure_phase = "stream_completion"
    elif isinstance(transport_error, httpx2.ProxyError):
        failure_phase = "proxy_connect"
    elif isinstance(transport_error, (httpx2.ConnectError, httpx2.ConnectTimeout)):
        failure_phase = "connect"
    elif isinstance(transport_error, httpx2.LocalProtocolError):
        failure_phase = "request_write"
    elif isinstance(
        transport_error, (httpx2.ReadError, httpx2.ReadTimeout, httpx2.RemoteProtocolError)
    ):
        failure_phase = "response_read"
    else:
        failure_phase = "unknown"

    if code in {"content_filter", "content_filter_error", "content_policy_violation"} or any(
        isinstance(item, (ChatClientContentFilterException, ContentFilterFinishReasonError))
        for item in chain
    ):
        category = "content_filter"
    elif (
        status in {401, 403}
        or code
        in {
            "invalid_api_key",
            "authentication_error",
            "permission_denied",
        }
        or any(isinstance(item, ChatClientInvalidAuthException) for item in chain)
    ):
        category = "authentication"
    elif status == 402:
        category = "payment_required"
    elif (
        status == 400
        or code in {"invalid_request", "invalid_request_error"}
        or any(isinstance(item, ChatClientInvalidRequestException) for item in chain)
    ):
        category = "invalid_request"
    elif status == 429:
        category = "rate_limit"
    elif status == 408:
        category = "timeout"
    elif status == 409:
        category = "conflict"
    elif status is not None and status >= 500:
        category = "server_error"
    elif status is None and code in {"rate_limit", "rate_limit_exceeded"}:
        category = "rate_limit"
    elif status is None and code == "server_error":
        category = "server_error"
    elif stream_error is not None:
        category = stream_error.category
    elif any(isinstance(item, (APITimeoutError, httpx2.TimeoutException)) for item in chain):
        category = "timeout"
    elif any(isinstance(item, (APIConnectionError, httpx2.TransportError)) for item in chain):
        category = "connection"
    else:
        category = "unknown"

    text_chars = sum(
        len(content.text)
        for message in messages
        for content in message.contents
        if content.type == "text" and isinstance(content.text, str)
    )
    return {
        "category": category,
        "scope": "provider" if category in PROVIDER_ERROR_CATEGORIES else "request",
        "exception_type": _safe_exception_type(error) or "OtherException",
        "cause_type": next(
            (name for item in reversed(chain[1:]) if (name := _safe_exception_type(item))),
            None,
        ),
        "transport_type": transport_type,
        "failure_phase": failure_phase,
        "event_type": stream_error.event_type
        if stream_error and stream_error.event_type in _STREAM_EVENTS
        else None,
        "incomplete_reason": _bounded_identifier(stream_error.incomplete_reason, _IDENTIFIER)
        if stream_error is not None
        else None,
        "finish_reason": stream_error.finish_reason if stream_error else None,
        "provider_response_id": stream_error.provider_response_id if stream_error else None,
        "transient": category in {"connection", "timeout", "rate_limit", "conflict", "server_error"}
        and not any(
            isinstance(
                item,
                (
                    httpx2.LocalProtocolError,
                    TypeError,
                    ValueError,
                    ChatClientInvalidAuthException,
                    ChatClientInvalidRequestException,
                ),
            )
            or (isinstance(item, RuntimeError) and not isinstance(item, ModelStreamError))
            for item in chain
        ),
        "http_status": status,
        "provider_code": code
        or (
            _bounded_identifier(stream_error.provider_code, _IDENTIFIER)
            if stream_error is not None
            else None
        ),
        "request_id": request_id,
        "retry_after": retry_after,
        "elapsed_ms": max(0, min(elapsed_ms, 86_400_000)),
        "message_count": min(len(messages), 1_000_000),
        "text_chars": min(text_chars, 1_000_000_000),
        "attempt_limit": max(1, min(attempt_limit, 100)),
    }


def model_attempt_limit(provider: str) -> int:
    """Return the configured maximum number of provider HTTP attempts."""
    return 5 if provider in OPENAI_PROVIDERS else 1


def is_transient_model_error(error: BaseException) -> bool:
    """Recover provider failures, never a local error with incidental transport context."""
    return (
        isinstance(error, (ChatClientException, APIError, httpx2.TransportError, ModelStreamError))
        and model_error_metadata(error)["transient"] is True
    )
