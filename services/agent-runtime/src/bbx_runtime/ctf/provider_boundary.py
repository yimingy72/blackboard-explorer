"""Fence the pinned MAF leaf client without replacing its native tool loop."""

from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, cast

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Message,
    ResponseStream,
)


@contextmanager
def provider_call_boundary(
    client: BaseChatClient,
    before_send: Callable[[], Awaitable[None]],
    prepare_messages: Callable[[Sequence[Message]], list[Message]],
) -> Iterator[None]:
    """Mark dispatch only after MAF has prepared history and outgoing messages."""
    original = client._inner_get_response
    prior_override = vars(client).get("_inner_get_response")

    def guarded(**kwargs: Any) -> Any:
        if kwargs.get("stream", False):

            async def start() -> ResponseStream[ChatResponseUpdate, ChatResponse]:
                outgoing = prepare_messages(kwargs["messages"])
                await before_send()
                return cast(
                    ResponseStream[ChatResponseUpdate, ChatResponse],
                    original(**{**kwargs, "messages": outgoing}),
                )

            return ResponseStream.from_awaitable(start())

        async def respond() -> ChatResponse:
            outgoing = prepare_messages(kwargs["messages"])
            await before_send()
            return await cast(Awaitable[ChatResponse], original(**{**kwargs, "messages": outgoing}))

        return respond()

    client._inner_get_response = guarded
    try:
        yield
    finally:
        if prior_override is None:
            del client._inner_get_response
        else:
            client._inner_get_response = prior_override
