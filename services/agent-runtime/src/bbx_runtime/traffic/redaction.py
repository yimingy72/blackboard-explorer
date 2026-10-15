"""Bound and redact display copies; packet captures retain their original bytes."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LIMIT = 16 * 1024
SENSITIVE = re.compile(
    r"authorization|cookie|password|passwd|secret|api.?key|private.?key|access.?token|refresh.?token|credential|signature",
    re.I,
)
BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[a-zA-Z0-9+/_.=-]+")
ASSIGNMENT = re.compile(
    r"""(?i)((?:password|passwd|secret|api[_-]?key|access[_-]?token|authorization|cookie)[\w-]*["']?\s*[:=]\s*["']?)([^\s,"';}]+)"""
)


def bounded(value: str, limit: int = LIMIT) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def text(value: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
            # SDK span limits may cut an echoed credential at the end of an attribute.
            candidate = value.find(secret[:4], max(0, len(value) - len(secret) + 1))
            while len(secret) > 4 and candidate >= 0:
                if secret.startswith(value[candidate:]):
                    value = value[:candidate] + "[REDACTED]"
                    break
                candidate = value.find(secret[:4], candidate + 1)
    value = re.sub(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.S,
    )
    value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
    return bounded(ASSIGNMENT.sub(r"\1[REDACTED]", BEARER.sub(r"\1 [REDACTED]", value)))


def clean(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE.search(str(key)) else clean(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [clean(item, secrets) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return text(value, secrets)
        if isinstance(parsed, (dict, list)):
            return bounded(json.dumps(clean(parsed, secrets), ensure_ascii=False))
        return text(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return text(str(value), secrets)


def url(value: str, secrets: tuple[str, ...] = ()) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host += f":{parts.port}"
        query = urlencode(
            [
                (key, "[REDACTED]" if SENSITIVE.search(key) else text(item, secrets))
                for key, item in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return text(urlunsplit((parts.scheme, host, parts.path, query, "")), secrets)
    except ValueError:
        return "[invalid URL]"


def side(value: Any, secrets: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    headers = []
    remaining = LIMIT
    for pair in value.get("headers", []):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        key = bounded(str(pair[0]), min(remaining, 1024))
        item = "[REDACTED]" if SENSITIVE.search(key) else text(str(pair[1]), secrets)
        item = bounded(item, max(0, remaining - len(key.encode())))
        remaining -= len(key.encode()) + len(item.encode())
        headers.append([key, item])
        if remaining <= 0:
            break
    if value.get("body_encoding") == "base64":
        return {
            "headers": headers,
            "body_preview": "",
            "body_bytes": max(0, int(value.get("body_bytes", 0))),
            "body_encoding": "omitted",
            "body_truncated": True,
        }
    preview = str(value.get("body_preview", ""))
    original_length = len(preview.encode())
    redacted = clean(preview, secrets)
    preview = redacted if isinstance(redacted, str) else json.dumps(redacted, ensure_ascii=False)
    return {
        "headers": headers,
        "body_preview": bounded(preview),
        "body_bytes": max(0, int(value.get("body_bytes", 0))),
        "body_encoding": value.get("body_encoding", "utf-8"),
        "body_truncated": bool(value.get("body_truncated")) or original_length > LIMIT,
    }
