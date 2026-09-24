"""Process-local PostgreSQL notification fanout for SSE clients."""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import UUID

import asyncpg
from sse_starlette.sse import EventSourceResponse

from bbx_blackboard.settings import Settings


class SSEDispatcher:
    def __init__(
        self,
        settings: Settings,
        *,
        connect: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        async def database_connection():
            return await asyncpg.connect(
                host=settings.postgres_host,
                port=settings.postgres_port,
                user=settings.postgres_user,
                password=settings.postgres_password.get_secret_value(),
                database=settings.postgres_db,
            )

        self._connect = connect or database_connection
        self._subscribers: dict[str, set[asyncio.Queue[None]]] = defaultdict(set)
        self._listening: set[str] = set()
        self._connection: Any | None = None
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _notify(self, connection, pid: int, channel: str, payload: str) -> None:
        for queue in tuple(self._subscribers.get(channel, ())):
            if queue.empty():
                queue.put_nowait(None)

    def _wake_all(self) -> None:
        for channel in self._subscribers:
            self._notify(None, 0, channel, "")

    async def _run(self) -> None:
        while True:
            connection = None
            try:
                connection = await self._connect()
                terminated = asyncio.get_running_loop().create_future()

                def on_termination(_, done=terminated):
                    if not done.done():
                        done.set_result(None)

                connection.add_termination_listener(on_termination)
                async with self._lock:
                    for channel in self._subscribers:
                        await connection.add_listener(channel, self._notify)
                        self._listening.add(channel)
                    self._connection = connection
                    self._ready.set()
                    self._wake_all()
                await terminated
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)
            finally:
                async with self._lock:
                    self._connection = None
                    self._ready.clear()
                    self._listening.clear()
                if connection is not None:
                    with suppress(Exception):
                        await connection.close()

    @asynccontextmanager
    async def subscribe(self, tid: UUID) -> AsyncIterator[asyncio.Queue[None]]:
        channel = f"bbx_task_{tid.hex}"
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        added = False
        try:
            async with self._lock:
                self._subscribers[channel].add(queue)
                added = True
                if self._connection is not None and channel not in self._listening:
                    await self._connection.add_listener(channel, self._notify)
                    self._listening.add(channel)
            await self._ready.wait()
            yield queue
        finally:
            if added:
                async with self._lock:
                    subscribers = self._subscribers[channel]
                    subscribers.discard(queue)
                    if not subscribers:
                        del self._subscribers[channel]
                        if self._connection is not None and channel in self._listening:
                            with suppress(Exception):
                                await self._connection.remove_listener(channel, self._notify)
                            self._listening.discard(channel)


async def stream_events(
    service: Any,
    dispatcher: SSEDispatcher,
    tid: UUID,
    since: int = 0,
    for_agent: str | None = None,
) -> AsyncGenerator[dict[str, str], None]:
    cursor = since
    async with dispatcher.subscribe(tid) as wake:
        while True:
            for event in await service.events(tid, cursor, for_agent=for_agent):
                version = int(event["version"])
                if version <= cursor:
                    continue
                cursor = version
                yield {
                    "id": str(version),
                    "event": event["type"],
                    "data": json.dumps(event, ensure_ascii=False, default=str),
                }
            await wake.get()


def stream_response(
    service: Any,
    dispatcher: SSEDispatcher,
    tid: UUID,
    since: int = 0,
    for_agent: str | None = None,
) -> EventSourceResponse:
    return EventSourceResponse(stream_events(service, dispatcher, tid, since, for_agent), ping=15)
