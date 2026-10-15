"""Bounded, redacted display copies of captured network data."""

import base64
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PREVIEW_BYTES = 16 * 1024
SENSITIVE = re.compile(
    r"authorization|authbasic|cookie|password|passwd|secret|token|api.?key|credential", re.I
)
ASSIGNMENT = re.compile(
    r"(?i)([\"\']?(?:password|passwd|secret|token|api[_-]?key|access_token|refresh_token)"
    r"[\"\']?\s*[:=]\s*)(\"[^\"]*\"|\'[^\']*\'|[^\s&,;}]+)"
)


def complete_wire_detail(detail: dict[str, Any]) -> None:
    """Include frozen DTO defaults before measuring immutable metadata batches.

    envd intentionally has no contracts service dependency. Contract comparison tests
    keep this wire boundary aligned with the shared model's full JSON expansion.
    """
    for field in (
        "record_kind",
        "packet",
        "transport_protocol",
        "application_protocol",
        "request",
        "response",
        "observation",
        "packet_detail",
    ):
        detail.setdefault(field, None)
    observation = detail.get("observation")
    if observation is not None:
        for field, default in (
            ("scope", "logical_call"),
            ("trace_id", None),
            ("span_id", None),
            ("parent_span_id", None),
            ("status", "unknown"),
            ("error", None),
            ("retry_visibility", "unknown"),
        ):
            observation.setdefault(field, default)
        observation.setdefault("attributes", {})
    packet_detail = detail.get("packet_detail")
    if packet_detail is not None:
        pending = list(packet_detail.get("layers", []))
        while pending:
            node = pending.pop()
            for field, default in (
                ("value", None),
                ("offset", None),
                ("length", None),
                ("data_source_id", 0),
                ("filter_expr", None),
                ("generated", False),
                ("severity", None),
            ):
                node.setdefault(field, default)
            node.setdefault("children", [])
            pending.extend(node["children"])


def redact_text(value: str) -> str:
    value = re.sub(
        r"(?im)(authorization|proxy-authorization|cookie|set-cookie):[^\r\n]*",
        r"\1: [redacted]",
        value,
    )
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", value)
    return ASSIGNMENT.sub(lambda match: match[1] + '"[redacted]"', value)


def redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        query = urlencode(
            [
                (key, "[redacted]" if SENSITIVE.search(key) else item)
                for key, item in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parts.scheme, host, redact_text(parts.path), query, ""))
    except ValueError:
        return "[invalid URL]"


def message_preview(headers: list[tuple[str, str]], body: bytes, total: int) -> dict[str, object]:
    shown: list[list[str]] = []
    remaining = PREVIEW_BYTES
    for name, value in headers[:256]:
        if remaining <= 0:
            break
        name = name[:256]
        value = "[redacted]" if SENSITIVE.search(name) else redact_text(value)
        value = value.encode("utf-8", "replace")[: max(0, remaining - len(name))].decode(
            "utf-8", "ignore"
        )
        shown.append([name, value])
        remaining -= len(name.encode()) + len(value.encode())
    sample = body[:PREVIEW_BYTES]
    try:
        text = sample.decode("utf-8")
        if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
            raise ValueError("binary body")
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            text = redact_text(text)
        else:

            def redact(item: object) -> object:
                if isinstance(item, dict):
                    return {
                        key: "[redacted]" if SENSITIVE.search(str(key)) else redact(value)
                        for key, value in item.items()
                    }
                if isinstance(item, list):
                    return [redact(value) for value in item]
                if isinstance(item, str):
                    return redact_text(item)
                return item

            text = json.dumps(redact(parsed), ensure_ascii=False)
        encoding = "utf-8"
        text = text.encode("utf-8")[:PREVIEW_BYTES].decode("utf-8", "ignore")
    except (UnicodeDecodeError, ValueError):
        sample = sample[: PREVIEW_BYTES * 3 // 4]
        text = base64.b64encode(sample).decode("ascii")
        encoding = "base64"
    return {
        "headers": shown,
        "body_preview": text,
        "body_bytes": total,
        "body_encoding": encoding,
        "body_truncated": total > len(sample),
    }
