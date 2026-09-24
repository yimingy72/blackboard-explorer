"""SSE wakeups, debounce, fallback ticks, and serialized task decisions."""

import asyncio
from typing import cast

from bbx_contracts.models import Params
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.scheduler import loop as loop_module
from bbx_runtime.scheduler.executor import ActionExecutor
from bbx_runtime.scheduler.loop import SchedulerLoop


class FakeStreamService:
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict | None] = asyncio.Queue()
        self.since: list[int] = []

    async def state(self, _task_id: str) -> dict:
        return {"task": {"status": "running"}, "agents": {}}

    async def stream(self, _task_id: str, since: int = 0):
        self.since.append(since)
        while True:
            event = await self.events.get()
            if event is None:
                return
            yield event


class FakeExecutor:
    task_id = "task"

    def __init__(self) -> None:
        self.ticks = 0
        self.active = 0
        self.max_active = 0
        self.shutdown_reasons: list[str] = []
        self.request_reasons: list[str] = []

    def request_stop(self, reason: str) -> None:
        self.request_reasons.append(reason)

    async def execute(self, _actions) -> None:
        self.ticks += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1

    async def shutdown(self, reason: str) -> None:
        self.shutdown_reasons.append(reason)


async def wait_ticks(executor: FakeExecutor, count: int) -> None:
    for _ in range(100):
        if executor.ticks >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Only {executor.ticks} ticks completed")


async def test_sse_burst_debounces_once_and_stop_keeps_reason(monkeypatch):
    monkeypatch.setattr(loop_module, "DEBOUNCE_SECONDS", 0.03)
    monkeypatch.setattr(loop_module, "FALLBACK_SECONDS", 0.5)
    monkeypatch.setattr(loop_module, "decide", lambda *_args: [])
    service, executor = FakeStreamService(), FakeExecutor()
    loop = SchedulerLoop(cast(ActionExecutor, executor), cast(BlackboardClient, service), Params())
    running = asyncio.create_task(loop.run())
    await wait_ticks(executor, 1)
    await service.events.put({"version": 1, "type": "fact.posted"})
    await service.events.put({"version": 2, "type": "intent.posted"})
    await wait_ticks(executor, 2)
    await asyncio.sleep(0.08)
    assert executor.ticks == 2
    assert loop._last_version == 2
    await loop.stop(reason="grace_timeout")
    await asyncio.wait_for(running, 2)
    assert executor.shutdown_reasons == ["grace_timeout"]
    assert loop._sweeper_task is not None and loop._sweeper_task.done()
    assert service.since == [0]


async def test_fallback_ticks_and_external_ticks_share_one_lock(monkeypatch):
    monkeypatch.setattr(loop_module, "FALLBACK_SECONDS", 0.05)
    monkeypatch.setattr(loop_module, "decide", lambda *_args: [])
    service, executor = FakeStreamService(), FakeExecutor()
    loop = SchedulerLoop(cast(ActionExecutor, executor), cast(BlackboardClient, service), Params())
    running = asyncio.create_task(loop.run())
    await wait_ticks(executor, 2)
    await asyncio.gather(loop.tick(), loop.tick())
    assert executor.max_active == 1
    await loop.stop()
    await asyncio.wait_for(running, 2)
    assert executor.shutdown_reasons == ["runtime_restart"]


async def test_continuous_sse_events_cannot_starve_two_second_fallback(monkeypatch):
    monkeypatch.setattr(loop_module, "DEBOUNCE_SECONDS", 0.04)
    monkeypatch.setattr(loop_module, "FALLBACK_SECONDS", 0.12)
    monkeypatch.setattr(loop_module, "decide", lambda *_args: [])
    service, executor = FakeStreamService(), FakeExecutor()
    loop = SchedulerLoop(cast(ActionExecutor, executor), cast(BlackboardClient, service), Params())
    running = asyncio.create_task(loop.run())
    await wait_ticks(executor, 1)
    for version in range(1, 31):
        await service.events.put({"version": version, "type": "fact.posted"})
        await asyncio.sleep(0.01)
    assert executor.ticks >= 3
    await loop.stop()
    await asyncio.wait_for(running, 2)


async def test_runtime_restart_overrides_pending_terminal_stop(monkeypatch):
    monkeypatch.setattr(loop_module, "decide", lambda *_args: [])
    service, executor = FakeStreamService(), FakeExecutor()
    loop = SchedulerLoop(cast(ActionExecutor, executor), cast(BlackboardClient, service), Params())
    running = asyncio.create_task(loop.run())
    await wait_ticks(executor, 1)
    loop.request_stop("grace_timeout")
    loop.request_stop("runtime_restart")
    await loop.stop("grace_timeout")
    await asyncio.wait_for(running, 2)
    assert executor.shutdown_reasons == ["runtime_restart"]
    assert executor.request_reasons[-1] == "runtime_restart"


async def test_stop_cancels_blocked_sweeper_before_executor_shutdown(monkeypatch):
    monkeypatch.setattr(loop_module, "decide", lambda *_args: [])
    service, executor = FakeStreamService(), FakeExecutor()
    loop = SchedulerLoop(cast(ActionExecutor, executor), cast(BlackboardClient, service), Params())
    started = asyncio.Event()

    async def blocked_sweeper() -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(loop._sweeper, "run", blocked_sweeper)
    running = asyncio.create_task(loop.run())
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(loop.stop("runtime_restart"), 0.5)
    await asyncio.wait_for(running, 2)
    assert executor.shutdown_reasons == ["runtime_restart"]
