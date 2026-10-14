"""CTF control wakes use durable SSE cursors with bounded polling compensation."""

import asyncio
from types import SimpleNamespace as NS
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.store.repository import Repository
from bbx_runtime.ctf.coordinator import CtfCoordinator, scheduling_event
from bbx_runtime.ctf.execution import CtfExecutionAdapter


class Events:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.cursors = []
        self.closed = 0

    async def stream(self, task_id, since):
        self.cursors.append(since)
        try:
            while True:
                event = await self.queue.get()
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            self.closed += 1


def runner():
    return NS(
        clear_platform_catalog=Mock(),
        retry_checkpoints=AsyncMock(),
        retry_settlements=AsyncMock(),
        pending_checkpoints={},
        pending_settlements={},
        envds={},
    )


def coordinator(events=None, **kwargs):
    value = CtfCoordinator(cast(Any, None), cast(Any, None), runner=runner(), **kwargs)
    value.service = cast(Any, NS(client=events or Events(), runtime=AsyncMock()))
    return value


async def until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate()


async def test_idle_sixty_seconds_uses_32_scans_and_avoids_history_replay(monkeypatch):
    from bbx_runtime.ctf import coordinator as module

    clock = 0.0
    scans = []
    events = Events()
    value = coordinator(events)
    value._start_with_recovery = AsyncMock()

    async def tick(task_id):
        scans.append(clock)
        value.event_cursors.setdefault(task_id, 50_000)
        return clock < 60

    async def wait(awaitable, timeout):
        nonlocal clock
        awaitable.close()
        clock += timeout
        await asyncio.sleep(0)
        raise TimeoutError

    value.tick = tick
    monkeypatch.setattr(module, "monotonic", lambda: clock)
    monkeypatch.setattr(module.asyncio, "wait_for", wait)
    await value.run("task")
    assert len(scans) == 32
    assert scans[:3] == [0, 0, 2]
    assert events.cursors == [50_000]
    assert events.closed == 1
    assert value.listeners == value.event_wakes == {}
    cast(Any, value.runner).clear_platform_catalog.assert_called_with("task")


@pytest.mark.parametrize("kind", ["ctf.message.posted", "ctf.conclusion.requested"])
async def test_message_and_close_wake_after_one_fixed_burst_window(monkeypatch, kind):
    from bbx_runtime.ctf import coordinator as module

    clock = 0.0
    scans = []
    events = Events()
    value = coordinator(events)
    value._start_with_recovery = AsyncMock()
    original_wait = asyncio.wait_for

    async def tick(task_id):
        scans.append(clock)
        value.event_cursors.setdefault(task_id, 90_000)
        return len(scans) < 3

    async def wait(awaitable, timeout):
        nonlocal clock
        if timeout == 2:
            clock = 0.01
            events.queue.put_nowait({"version": 90_001, "type": kind})
            return await original_wait(awaitable, timeout=1)
        awaitable.close()
        assert timeout == pytest.approx(0.2)
        # Further notifications arrive during the burst without extending its deadline.
        for version in range(90_002, 90_102):
            events.queue.put_nowait({"version": version, "type": kind})
        clock += timeout
        await asyncio.sleep(0)
        raise TimeoutError

    value.tick = tick
    monkeypatch.setattr(module, "monotonic", lambda: clock)
    monkeypatch.setattr(module.asyncio, "wait_for", wait)
    await value.run("task")
    assert scans == pytest.approx([0, 0, 0.21])
    assert events.closed == 1


async def test_trace_storm_advances_cursor_without_waking_and_reconnect_preserves_cursor(
    monkeypatch,
):
    from bbx_runtime.ctf import coordinator as module

    events = Events()
    value = coordinator(events)
    value.event_wakes["task"] = asyncio.Event()
    value.event_cursors["task"] = 8_000
    monkeypatch.setattr(module, "EVENT_RECONNECT_SECONDS", 0)
    listener = asyncio.create_task(value._listen("task"))
    value.listeners["task"] = listener
    for version in range(8_001, 9_001):
        events.queue.put_nowait({"version": version, "type": "agent.trace.recorded"})
    await until(lambda: value.event_cursors["task"] == 9_000)
    assert not value.event_wakes["task"].is_set()
    events.queue.put_nowait(httpx.ReadError("synthetic stream drop"))
    await until(lambda: len(events.cursors) == 2)
    assert events.cursors == [8_000, 9_000]
    events.queue.put_nowait({"version": 9_000, "type": "ctf.message.posted"})
    events.queue.put_nowait({"version": 9_001, "type": "ctf.message.posted"})
    await until(lambda: value.event_wakes["task"].is_set())
    assert value.event_cursors["task"] == 9_001
    await value.close()
    assert events.closed == 2
    assert listener.done()


async def test_tick_subscription_gap_is_replayed_from_initial_lower_watermark(monkeypatch):
    events = Events()
    value = coordinator(events, poll_seconds=0)
    value._start_with_recovery = AsyncMock()
    scans = 0

    async def tick(task_id):
        nonlocal scans
        scans += 1
        value.event_cursors.setdefault(task_id, 15_000)
        if scans == 1:
            # A commit after the first state read must be reachable from its earlier cursor.
            events.queue.put_nowait({"version": 15_001, "type": "ctf.message.posted"})
        await asyncio.sleep(0)
        return scans < 3

    value.tick = tick
    await value.run("task")
    assert events.cursors == [15_000]
    assert scans == 3


async def test_event_burst_cannot_shorten_control_transport_backoff(monkeypatch):
    from bbx_runtime.ctf import coordinator as module

    value = coordinator()
    value._start_with_recovery = AsyncMock()
    scans, delays = [], []

    async def tick(task_id):
        scans.append(True)
        if len(scans) == 1:
            value.wake(task_id)
            raise httpx.ReadError("synthetic control outage")
        return False

    async def wait(awaitable, timeout):
        assert awaitable.cr_frame.f_locals["self"] is value.stopping
        awaitable.close()
        delays.append(timeout)
        raise TimeoutError

    value.tick = tick
    monkeypatch.setattr(module.asyncio, "wait_for", wait)
    await value.run("task")
    assert len(scans) == 2
    assert delays == [1]


async def test_future_completion_and_explicit_stop_wake_without_waiting_for_poll():
    value = coordinator()
    value.event_wakes["task"] = asyncio.Event()
    cast(Any, value.runner).run = AsyncMock()
    value.service.runtime_state = AsyncMock(
        return_value={
            "event_version": 300,
            "task": {
                "ctf_control": {"phase": "running"},
                "budget": {"max_cost": "10", "max_minutes": 10},
            },
            "members": [{"id": "lead", "generation": 1, "lifecycle": "active"}],
            "ready_member_ids": ["lead"],
        }
    )
    value.service.runtime = AsyncMock(return_value={"id": "turn", "generation": 1})
    assert await value.tick("task")
    await until(lambda: value.active[("task", "lead")][0].done())
    await until(lambda: value.event_wakes["task"].is_set())
    assert value.event_wakes["task"].is_set()
    value.event_wakes["task"].clear()
    await value.stop("task")
    assert value.event_wakes["task"].is_set()


@pytest.mark.parametrize("operation", ["detach", "close", "cancel"])
async def test_listener_is_joined_and_catalog_cleared_on_every_exit(operation):
    events = Events()
    value = coordinator(events)
    value._start_with_recovery = AsyncMock()

    async def tick(task_id):
        value.event_cursors.setdefault(task_id, 6_000)
        return True

    value.tick = tick
    work = asyncio.create_task(value.run("task"))
    await until(lambda: bool(events.cursors))
    listener = value.listeners["task"]
    if operation == "detach":
        await value.detach("task", deleting=True)
    elif operation == "close":
        await value.close()
    else:
        work.cancel()
    result = await asyncio.gather(work, return_exceptions=True)
    assert operation != "cancel" or isinstance(result[0], asyncio.CancelledError)
    assert listener.done() and events.closed == 1
    assert value.listeners == value.event_wakes == {}
    cast(Any, value.runner).clear_platform_catalog.assert_called_with("task")


async def test_health_checks_throttle_only_unchanged_generation_hints(monkeypatch):
    from bbx_runtime.ctf import coordinator as module

    clock = 0.0
    status = AsyncMock(return_value={"state": "ready", "boot_id": "boot"})
    value = coordinator(envd=CtfExecutionAdapter(cast(Any, None), cast(Any, NS(ctf_status=status))))
    state = {
        "event_version": 300,
        "task": {
            "ctf_control": {"phase": "running"},
            "budget": {"max_cost": "10", "max_minutes": 10},
        },
        "members": [],
        "ready_member_ids": [],
    }
    value.service.runtime_state = AsyncMock(side_effect=lambda _: state)
    monkeypatch.setattr(module, "monotonic", lambda: clock)
    for index in range(10):
        clock = index / 10
        assert await value.tick("task")
    assert status.await_count == 1
    state["members"] = [{"id": "member", "generation": 2, "lifecycle": "removed"}]
    assert await value.tick("task")
    assert status.await_count == 2
    clock += 2
    assert await value.tick("task")
    assert status.await_count == 3


def test_event_filter_ignores_observations_records_and_layout():
    for kind in (
        "agent.trace.recorded",
        "tool_call.recorded",
        "ctf.record.appended",
        "ctf.challenge.updated",
    ):
        assert not scheduling_event(kind)
    for kind in (
        "ctf.message.posted",
        "ctf.member.state_changed",
        "ctf.turn.finished",
        "ctf.conclusion.requested",
    ):
        assert scheduling_event(kind)


async def test_runtime_cursor_is_read_before_scheduling_state(monkeypatch):
    from bbx_blackboard import ctf as module

    reads = []

    class Rows:
        def scalar_one(self):
            return 50_000

        def mappings(self):
            return self

        def __iter__(self):
            return iter([])

        def all(self):
            return []

    async def execute(query):
        reads.append("cursor" if "max(events.version)" in str(query) else "view")
        return Rows()

    async def task_row(*_):
        reads.append("task")
        return {"mode": "ctf", "ctf_control": {}}

    connection = NS(execute=execute)
    context = AsyncMock()
    context.__aenter__.return_value = connection
    engine = Mock()
    engine.connect.return_value = context
    monkeypatch.setattr(module, "row", task_row)
    result = await CtfService(engine).runtime_state(uuid4())
    assert result["event_version"] == 50_000
    assert reads == ["cursor", "task", "view", "view", "view"]


async def test_ctf_emit_and_board_notify_on_same_transaction_connection():
    tid, connection = uuid4(), NS(execute=AsyncMock())
    engine = Mock()
    repo = Repository(engine)
    await repo.notify(tid, 77, connection=cast(Any, connection))
    engine.begin.assert_not_called()
    params = connection.execute.call_args.args[1]
    assert params == {"channel": f"bbx_task_{tid.hex}", "version": "77"}
    context = AsyncMock()
    context.__aenter__.return_value = connection
    engine.begin.return_value = context
    await repo.notify(tid, 78)
    engine.begin.assert_called_once_with()
    assert connection.execute.call_args.args[1]["version"] == "78"
    backend = CtfService(engine)
    backend.repo.append = AsyncMock(return_value=[{"version": 77}])
    backend.repo.notify = AsyncMock()
    await backend._emit(connection, tid, "control.changed", "system", {})
    backend.repo.notify.assert_awaited_once_with(tid, 77, connection=connection)
    backend.repo.notify.reset_mock()
    await backend._board_event(
        connection, tid, "system", "ctf.challenge.updated", "id", "digest", {"id": "c"}
    )
    backend.repo.notify.assert_awaited_once_with(tid, 77, connection=connection)
