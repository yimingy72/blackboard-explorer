"""Bounded, content-free diagnostics for failed model requests."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any

from agent_framework import Message
from agent_framework.exceptions import (
    ChatClientContentFilterException,
    ChatClientInvalidAuthException,
    ChatClientInvalidRequestException,
)
from openai import (
    APIConnectionError,
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
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SECONDS = re.compile(r"[0-9]{1,8}(?:\.[0-9]{1,3})?\Z")


def _bounded_identifier(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


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
    elif any(isinstance(item, APITimeoutError) for item in chain):
        category = "timeout"
    elif any(isinstance(item, APIConnectionError) for item in chain):
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
        "transient": category
        in {"connection", "timeout", "rate_limit", "conflict", "server_error"},
        "http_status": status,
        "provider_code": code,
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
