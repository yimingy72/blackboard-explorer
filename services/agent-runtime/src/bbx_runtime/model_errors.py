"""Bounded, content-free diagnostics for failed model requests."""

from __future__ import annotations

import re
from collections.abc import Sequence
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


class ModelStreamError(RuntimeError):
    """A provider terminal event that cannot be used as a complete model turn."""

    def __init__(
        self,
        category: str,
        *,
        event_type: str | None = None,
        provider_code: str | None = None,
        incomplete_reason: str | None = None,
    ) -> None:
        self.category = category
        self.event_type = (
            event_type if isinstance(event_type, str) and event_type in _STREAM_EVENTS else None
        )
        self.provider_code = _bounded_identifier(provider_code, _IDENTIFIER)
        self.incomplete_reason = _bounded_identifier(incomplete_reason, _IDENTIFIER)
        super().__init__("Model stream did not complete successfully")


class IncompleteModelStreamError(httpx2.RemoteProtocolError):
    """A locally detected stream EOF without a valid completion event."""


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SECONDS = re.compile(r"[0-9]{1,8}(?:\.[0-9]{1,3})?\Z")
_STREAM_EVENTS = {"response.failed", "response.incomplete", "error"}
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
    if status_error is not None:
        code = _bounded_identifier(status_error.code, _IDENTIFIER)
        body = status_error.body
        if code is None and isinstance(body, dict):
            detail = body.get("error")
            if isinstance(detail, dict):
                code = _bounded_identifier(detail.get("code"), _IDENTIFIER)
        request_id = _bounded_identifier(status_error.request_id, _REQUEST_ID)
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
    if status is not None:
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
    elif status == 429:
        category = "rate_limit"
    elif status == 408:
        category = "timeout"
    elif status == 409:
        category = "conflict"
    elif status is not None and status >= 500:
        category = "server_error"
    elif status in {401, 403} or any(
        isinstance(item, ChatClientInvalidAuthException) for item in chain
    ):
        category = "authentication"
    elif status == 402:
        category = "payment_required"
    elif status == 400 or any(
        isinstance(item, ChatClientInvalidRequestException) for item in chain
    ):
        category = "invalid_request"
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
        "transient": category
        in {"connection", "timeout", "rate_limit", "conflict", "server_error"},
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
