"""SSE delivery from a fake PostgreSQL notification connection."""

import asyncio
import json
from collections.abc import Callable
from uuid import uuid4

import pytest
from bbx_blackboard.settings import Settings
from bbx_blackboard.sse import SSEDispatcher, stream_events, stream_response


class FakeConnection:
    def __init__(self) -> None:
        self.listeners: dict[str, Callable] = {}
        self.termination: list[Callable] = []
        self.closed = False

    async def add_listener(self, channel: str, callback: Callable) -> None:
        self.listeners[channel] = callback

    async def remove_listener(self, channel: str, callback: Callable) -> None:
        del self.listeners[channel]

    def add_termination_listener(self, callback: Callable) -> None:
        self.termination.append(callback)

    def emit(self, channel: str) -> None:
        self.listeners[channel](self, 1, channel, "999")

    def fail(self) -> None:
        for callback in self.termination:
            callback(self)

    async def close(self) -> None:
        self.closed = True


class FakeService:
    def __init__(self, listener: FakeConnection, channel: str) -> None:
        self.listener = listener
        self.channel = channel
        self.events_log: list[dict] = []
        self.queries: list[tuple[int, str | None]] = []

    async def events(self, tid, since: int, for_agent: str | None = None) -> list[dict]:
        assert self.channel in self.listener.listeners
        self.queries.append((since, for_agent))
        return [event for event in self.events_log if event["version"] > since]


async def until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition did not become true")


@pytest.mark.asyncio
async def test_sse_subscribes_before_backfill_and_uses_event_versions() -> None:
    tid = uuid4()
    channel = f"bbx_task_{tid.hex}"
    connection = FakeConnection()

    async def connect():
        return connection

    dispatcher = SSEDispatcher(Settings.model_construct(), connect=connect)
    service = FakeService(connection, channel)
    service.events_log.append({"version": 1, "type": "fact.posted", "payload": {"id": "F1"}})
    await dispatcher.start()
    stream = stream_events(service, dispatcher, tid, since=0, for_agent="agent-1")
    try:
        first = await anext(stream)
        assert first["id"] == "1" and first["event"] == "fact.posted"
        assert json.loads(first["data"])["payload"] == {"id": "F1"}
        assert service.queries[0] == (0, "agent-1")

        service.events_log.extend(
            [
                {"version": 2, "type": "intent.posted", "payload": {}},
                {"version": 3, "type": "fact.posted", "payload": {}},
            ]
        )
        connection.emit(channel)
        connection.emit(channel)
        assert (await anext(stream))["id"] == "2"
        assert (await anext(stream))["id"] == "3"

        async with dispatcher.subscribe(uuid4()) as wake:
            assert len(connection.listeners) == 2
            assert wake.empty()
        assert list(connection.listeners) == [channel]
        assert stream_response(service, dispatcher, tid).ping_interval == 15
    finally:
        await stream.aclose()
        await dispatcher.stop()
    assert connection.closed
    assert not connection.listeners


@pytest.mark.asyncio
async def test_sse_reconnects_and_backfills_without_notification() -> None:
    tid = uuid4()
    channel = f"bbx_task_{tid.hex}"
    first = FakeConnection()
    second = FakeConnection()
    connections = iter((first, second))

    async def connect():
        return next(connections)

    dispatcher = SSEDispatcher(Settings.model_construct(), connect=connect)
    service = FakeService(first, channel)
    service.events_log.append({"version": 5, "type": "task.created", "payload": {}})
    await dispatcher.start()
    stream = stream_events(service, dispatcher, tid, since=4)
    try:
        assert (await anext(stream))["id"] == "5"
        service.events_log.append({"version": 6, "type": "fact.posted", "payload": {}})
        first.fail()
        await until(lambda: channel in second.listeners)
        service.listener = second
        assert (await anext(stream))["id"] == "6"
        assert service.queries[-1] == (5, None)
    finally:
        await stream.aclose()
        await dispatcher.stop()
    assert first.closed and second.closed


@pytest.mark.asyncio
async def test_sse_wakes_only_subscribers_of_notified_task() -> None:
    first_tid, second_tid = uuid4(), uuid4()
    connection = FakeConnection()

    async def connect():
        return connection

    dispatcher = SSEDispatcher(Settings.model_construct(), connect=connect)
    await dispatcher.start()
    try:
        async with (
            dispatcher.subscribe(first_tid) as first,
            dispatcher.subscribe(second_tid) as second,
        ):
            while not first.empty():
                first.get_nowait()
            while not second.empty():
                second.get_nowait()
            connection.emit(f"bbx_task_{second_tid.hex}")
            assert first.empty()
            assert second.get_nowait() is None
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_sse_catches_notification_during_backfill_query() -> None:
    tid = uuid4()
    channel = f"bbx_task_{tid.hex}"
    connection = FakeConnection()

    async def connect():
        return connection

    class RacingService(FakeService):
        async def events(self, tid, since: int, for_agent: str | None = None) -> list[dict]:
            result = await super().events(tid, since, for_agent)
            if len(self.queries) == 1:
                self.events_log.append({"version": 2, "type": "fact.posted", "payload": {}})
                connection.emit(channel)
            return result

    service = RacingService(connection, channel)
    service.events_log.append({"version": 1, "type": "task.created", "payload": {}})
    dispatcher = SSEDispatcher(Settings.model_construct(), connect=connect)
    await dispatcher.start()
    stream = stream_events(service, dispatcher, tid)
    try:
        assert (await anext(stream))["id"] == "1"
        assert (await anext(stream))["id"] == "2"
    finally:
        await stream.aclose()
        await dispatcher.stop()
