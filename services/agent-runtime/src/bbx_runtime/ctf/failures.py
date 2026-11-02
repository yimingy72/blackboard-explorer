"""Carry CTF tool failures across the native Pi callback boundary."""

from collections.abc import Iterator
from contextlib import contextmanager

from bbx_runtime.pi import MiddlewareFailure


class CtfToolFailure(MiddlewareFailure):
    """Stop native tool invocation without exposing the original error to the model."""

    def __init__(self, original: Exception) -> None:
        super().__init__("CTF tool execution must stop; the outcome requires platform recovery.")
        self.original_error = original


def tool_failure_cause(error: BaseException) -> Exception | None:
    """Unwrap only our explicit tool boundary, never arbitrary provider cause chains."""
    return error.original_error if isinstance(error, CtfToolFailure) else None


def grouped_failure_types(error: BaseException) -> list[str]:
    """Expose bounded exception type names, never exception messages or RPC payloads."""
    if not isinstance(error, BaseExceptionGroup):
        return []
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    names: list[str] = []
    while pending and len(seen) < 32 and len(names) < 8:
        item = pending.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions[:8])
            continue
        original = tool_failure_cause(item)
        if original is not None:
            pending.append(original)
            continue
        name = type(item).__name__
        name = (
            name
            if name.isascii() and name.isidentifier() and len(name) <= 100
            else "OtherException"
        )
        if name not in names:
            names.append(name)
    return names


@contextmanager
def propagate_tool_failure() -> Iterator[None]:
    """Restore the original exception for the runner's existing settlement handlers."""
    try:
        yield
    except CtfToolFailure as error:
        raise error.original_error from error.original_error.__cause__
