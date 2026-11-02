"""Stable platform error categories independent of provider SDK internals."""

from typing import Any


class ChatClientException(Exception):
    def __init__(self, message: str, *args: Any, inner_exception=None, **_kwargs: Any) -> None:
        super().__init__(message, *args)
        if inner_exception is not None:
            self.__cause__ = inner_exception


class ChatClientInvalidRequestException(ChatClientException):
    pass


class ChatClientInvalidAuthException(ChatClientException):
    pass


class ChatClientContentFilterException(ChatClientException):
    pass
