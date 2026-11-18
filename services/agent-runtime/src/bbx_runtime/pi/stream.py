"""Adapt Pi events to the platform's asynchronous stream consumers."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from bbx_runtime.pi.data import ChatResponse, ChatResponseUpdate, Content, Message


class ResponseStream[Update, Result]:
    def __init__(
        self,
        source: AsyncIterator[Update],
        *,
        finalizer: Callable | None = None,
        retain_updates: bool = True,
    ) -> None:
        self.source = source
        self.finalizer = finalizer
        self.retain_updates = retain_updates
        self.transforms: list[Callable] = []
        self.results: list[Callable] = []
        self.cleanups: list[Callable] = []
        self._items: list[Any] = []
        self._value: Any = None
        self._done = False
        self._error: BaseException | None = None
        self._iterating = False

    @classmethod
    def from_awaitable(cls, source: Awaitable[ResponseStream]) -> ResponseStream:
        holder: list[ResponseStream] = []

        async def updates():
            inner = await source
            holder.append(inner)
            async for update in inner:
                yield update

        async def finish(_items):
            return await holder[0].get_final_response()

        return cls(updates(), finalizer=finish, retain_updates=False)

    def with_transform_hook(self, hook: Callable) -> ResponseStream:
        self.transforms.append(hook)
        return self

    def with_result_hook(self, hook: Callable) -> ResponseStream:
        self.results.append(hook)
        return self

    def with_cleanup_hook(self, hook: Callable) -> ResponseStream:
        self.cleanups.append(hook)
        return self

    async def _apply(self, fn: Callable, value: Any) -> Any:
        result = fn(value)
        return await result if inspect.isawaitable(result) else result

    async def _iterate(self):
        if self._done:
            if self._error:
                raise self._error
            return
        if self._iterating:
            raise RuntimeError("A model stream can only have one consumer")
        self._iterating = True
        try:
            async for value in self.source:
                for hook in self.transforms:
                    value = await self._apply(hook, value)
                if self.retain_updates:
                    self._items.append(value)
                yield value
            for hook in self.cleanups:
                await self._apply(hook, self._items)
            self._value = (
                await self._apply(self.finalizer, self._items)
                if self.finalizer
                else response_from_updates(self._items)
            )
            for hook in self.results:
                updated = await self._apply(hook, self._value)
                if updated is not None:
                    self._value = updated
        except BaseException as error:
            self._error = error
            raise
        finally:
            self._iterating = False
            self._done = True

    def __aiter__(self):
        return self._iterate()

    async def get_final_response(self) -> Result:
        if not self._done:
            async for _ in self:
                pass
        if self._error:
            raise self._error
        return self._value

    def __await__(self):
        return self.get_final_response().__await__()


def response_from_updates(updates: list[ChatResponseUpdate]) -> ChatResponse:
    contents: list[Content] = []
    usage: dict[str, Any] = {}
    response_id = None
    finish_reason = None
    for update in updates:
        response_id = update.response_id or response_id
        finish_reason = update.finish_reason or finish_reason
        for content in update.contents:
            if content.type == "usage":
                usage.update(content.usage_details or {})
            elif content.type == "text" and contents and contents[-1].type == "text":
                contents[-1].text = (contents[-1].text or "") + (content.text or "")
            else:
                contents.append(content.model_copy(deep=True))
    return ChatResponse(
        messages=[Message("assistant", contents=contents)],
        response_id=response_id,
        usage_details=usage or None,
        finish_reason=finish_reason,
    )


def queued_stream[Result](producer: Callable[[asyncio.Queue], Awaitable[Result]]) -> ResponseStream:
    queue: asyncio.Queue = asyncio.Queue(maxsize=128)
    task: asyncio.Task | None = None
    sentinel = object()

    async def run():
        try:
            return await producer(queue)
        finally:
            running = asyncio.current_task()
            if running is not None and running.cancelling():
                try:
                    queue.put_nowait(sentinel)
                except asyncio.QueueFull:
                    pass
            else:
                await queue.put(sentinel)

    async def updates():
        nonlocal task
        current = asyncio.current_task()
        task = asyncio.create_task(run(), name=current.get_name() if current else None)
        try:
            while (value := await queue.get()) is not sentinel:
                yield value
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def finish(_items):
        assert task is not None
        return await task

    return ResponseStream(updates(), finalizer=finish, retain_updates=False)
