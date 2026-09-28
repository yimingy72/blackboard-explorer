"""Normalize values that cross PostgreSQL text and JSONB boundaries."""

from typing import Any


def storage_safe(value: Any) -> Any:
    """Copy JSON containers and render PostgreSQL's unsupported U+0000 visibly."""
    if isinstance(value, str):
        return value.replace(chr(0), r"\u0000")
    if isinstance(value, dict):
        return {storage_safe(key): storage_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [storage_safe(item) for item in value]
    return value
