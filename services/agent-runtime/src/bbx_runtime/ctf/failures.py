"""Carry CTF tool failures through MAF's public fail-closed boundary."""

from collections.abc import Iterator
from contextlib import contextmanager

from agent_framework import MiddlewareFailure


class CtfToolFailure(MiddlewareFailure):
    """Stop native tool invocation without exposing the original error to the model."""

    def __init__(self, original: Exception) -> None:
        super().__init__("CTF tool execution must stop; the outcome requires platform recovery.")
        self.original_error = original


def tool_failure_cause(error: BaseException) -> Exception | None:
    """Unwrap only our explicit tool boundary, never arbitrary provider cause chains."""
    return error.original_error if isinstance(error, CtfToolFailure) else None


@contextmanager
def propagate_tool_failure() -> Iterator[None]:
    """Restore the original exception for the runner's existing settlement handlers."""
    try:
        yield
    except CtfToolFailure as error:
        raise error.original_error from error.original_error.__cause__
