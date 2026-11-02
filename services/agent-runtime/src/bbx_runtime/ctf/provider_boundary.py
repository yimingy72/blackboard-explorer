"""Fence business input before Pi's native payload dispatch boundary."""

from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, cast

from bbx_runtime.pi import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Message,
    PiChatClient,
    ResponseStream,
)


@contextmanager
def provider_call_boundary(
    client: BaseChatClient,
    before_send: Callable[[], Awaitable[None]],
    prepare_messages: Callable[[Sequence[Message]], list[Message]],
) -> Iterator[None]:
    """Mark dispatch at Pi's payload boundary after history preparation."""
    original = client._inner_get_response
    prior_override = vars(client).get("_inner_get_response")
    prior_before_send = client.before_send
    native = isinstance(client, PiChatClient)
    if native:
        client.before_send = before_send

    def guarded(**kwargs: Any) -> Any:
        if kwargs.get("stream", False):

            async def start() -> ResponseStream[ChatResponseUpdate, ChatResponse]:
                outgoing = prepare_messages(kwargs["messages"])
                if not native:
                    await before_send()
                return cast(
                    ResponseStream[ChatResponseUpdate, ChatResponse],
                    original(**{**kwargs, "messages": outgoing}),
                )

            return ResponseStream.from_awaitable(start())

        async def respond() -> ChatResponse:
            outgoing = prepare_messages(kwargs["messages"])
            if not native:
                await before_send()
            return await cast(Awaitable[ChatResponse], original(**{**kwargs, "messages": outgoing}))

        return respond()

    client._inner_get_response = guarded
    try:
        yield
    finally:
        client.before_send = prior_before_send
        if prior_override is None:
            del client._inner_get_response
        else:
            client._inner_get_response = prior_override
