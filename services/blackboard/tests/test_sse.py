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


@pytest.mark.asyncio
async def test_optional_subscription_does_not_wait_for_listener_ready() -> None:
    dispatcher = SSEDispatcher(Settings.model_construct())
    tid = uuid4()
    channel = f"bbx_task_{tid.hex}"

    async def subscribe():
        async with dispatcher.subscribe(tid):
            pytest.fail("default SSE subscription must wait for readiness")

    waiting = asyncio.create_task(subscribe())
    try:
        await until(lambda: channel in dispatcher._subscribers)
        async with dispatcher.subscribe(tid, wait_ready=False) as wake:
            assert wake.empty() and not waiting.done()
        assert len(dispatcher._subscribers[channel]) == 1
    finally:
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    assert not dispatcher._subscribers


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["register", "cleanup", "lock", "entry_lock"])
async def test_optional_subscription_bounds_listener_io_and_cleans_queues(blocked: str) -> None:
    tid = uuid4()
    channel = f"bbx_task_{tid.hex}"

    class StalledConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.server_listeners: set[str] = set()
            self.stalled = True
            self.uncertain = False

        async def add_listener(self, channel: str, callback: Callable) -> None:
            self.server_listeners.add(channel)
            if channel == f"bbx_task_{tid.hex}" and blocked == "register" and self.stalled:
                self.uncertain = True
                assert channel not in self.listeners
                await asyncio.Event().wait()
            await super().add_listener(channel, callback)

        async def remove_listener(self, channel: str, callback: Callable) -> None:
            if channel not in self.listeners:
                return
            del self.listeners[channel]
            if channel == f"bbx_task_{tid.hex}" and blocked == "cleanup" and self.stalled:
                self.uncertain = True
                assert channel in self.server_listeners
                await asyncio.Event().wait()
            self.server_listeners.discard(channel)

        def terminate(self) -> None:
            self.closed = True
            self.listeners.clear()
            self.server_listeners.clear()
            self.fail()

        async def close(self) -> None:
            self.terminate()

    first, second = StalledConnection(), StalledConnection()
    second.stalled = False
    connections = iter((first, second))

    async def connect():
        return next(connections)

    dispatcher = SSEDispatcher(Settings.model_construct(), connect=connect)
    sse_tid = uuid4()
    sse_channel = f"bbx_task_{sse_tid.hex}"
    service = FakeService(first, sse_channel)
    service.events_log.append({"version": 1, "type": "task.created", "payload": {}})
    stream = stream_events(service, dispatcher, sse_tid)
    await dispatcher.start()
    pending = None
    try:
        assert (await anext(stream))["id"] == "1"
        for wake in dispatcher._subscribers[sse_channel]:
            while not wake.empty():
                wake.get_nowait()
        uncertain = blocked in {"register", "cleanup"}
        if uncertain:
            service.listener = second
        service.events_log.append({"version": 2, "type": "fact.posted", "payload": {}})
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        assert not pending.done()
        try:
            if blocked == "entry_lock":
                await dispatcher._lock.acquire()
            async with asyncio.timeout(0.4):
                async with dispatcher.subscribe(tid, wait_ready=False):
                    if blocked == "lock":
                        await dispatcher._lock.acquire()
            assert channel not in dispatcher._subscribers
        finally:
            if dispatcher._lock.locked():
                dispatcher._lock.release()
        assert first.closed == uncertain and first.uncertain == uncertain
        if uncertain:
            assert not first.listeners and not first.server_listeners
        else:
            assert not pending.done()
            first.stalled = False
            first.emit(sse_channel)
        assert (await asyncio.wait_for(pending, 0.5))["id"] == "2"
        assert service.queries[-1] == (1, None)
        async with dispatcher.subscribe(uuid4(), wait_ready=False):
            assert channel not in first.server_listeners | second.server_listeners
            assert channel not in dispatcher._listening
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        await stream.aclose()
        await dispatcher.stop()
    assert not first.server_listeners and not second.server_listeners
    assert not dispatcher._subscribers
