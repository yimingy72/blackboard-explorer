"""SSE wakeups, debounce, fallback ticks, and serialized task decisions."""

import asyncio
from typing import cast

import httpx
import pytest
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


async def test_blackboard_read_disconnect_waits_for_next_tick_without_stopping_agents(
    monkeypatch, caplog
):
    monkeypatch.setattr(loop_module, "FALLBACK_SECONDS", 0.03)
    monkeypatch.setattr(loop_module, "decide", lambda *_args: [])

    class FlakyRead(FakeStreamService):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        async def state(self, task_id: str) -> dict:
            self.reads += 1
            if self.reads == 1:
                raise httpx.RemoteProtocolError("lost internal response")
            return await super().state(task_id)

    service, executor = FlakyRead(), FakeExecutor()
    loop = SchedulerLoop(cast(ActionExecutor, executor), cast(BlackboardClient, service), Params())

    async def idle_sweeper() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(loop._sweeper, "run", idle_sweeper)
    agent = asyncio.create_task(asyncio.Event().wait())
    running = asyncio.create_task(loop.run())
    try:
        await wait_ticks(executor, 1)
        assert service.reads >= 2
        assert not running.done() and not agent.done()
        assert executor.request_reasons == []
        assert executor.shutdown_reasons == []
        assert "phase=read_state error=RemoteProtocolError" in caplog.text
        assert "lost internal response" not in caplog.text
    finally:
        await loop.stop()
        await asyncio.wait_for(running, 2)
        agent.cancel()
        await asyncio.gather(agent, return_exceptions=True)


@pytest.mark.parametrize(
    "control_url",
    ["http://blackboard.test/api/tasks/task/agents", "http://envd.test/users"],
)
async def test_lost_write_reply_rechecks_board_before_any_new_action(
    monkeypatch, caplog, control_url
):
    monkeypatch.setattr(loop_module, "FALLBACK_SECONDS", 0.03)

    class CommittedBoard(FakeStreamService):
        def __init__(self) -> None:
            super().__init__()
            self.committed = False
            self.reads = 0

        async def state(self, _task_id: str) -> dict:
            self.reads += 1
            return {
                "task": {"status": "running"},
                "agents": {"existing": {}} if self.committed else {},
            }

    service = CommittedBoard()

    class LostReply(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.actions: list[list[str]] = []

        async def execute(self, actions: list[str]) -> None:
            self.actions.append(actions)
            if actions:
                service.committed = True
                raise httpx.RemoteProtocolError(
                    "write committed, reply lost",
                    request=httpx.Request("POST", control_url),
                )
            self.ticks += 1

    executor = LostReply()
    monkeypatch.setattr(
        loop_module, "decide", lambda state, *_args: ["spawn"] if not state["agents"] else []
    )
    loop = SchedulerLoop(cast(ActionExecutor, executor), cast(BlackboardClient, service), Params())

    async def idle_sweeper() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(loop._sweeper, "run", idle_sweeper)
    running = asyncio.create_task(loop.run())
    try:
        await wait_ticks(executor, 1)
        assert service.reads >= 2
        assert executor.actions[:2] == [["spawn"], []]
        assert executor.actions.count(["spawn"]) == 1
        assert not running.done() and executor.shutdown_reasons == []
        assert "phase=apply_actions error=RemoteProtocolError" in caplog.text
        assert "write committed, reply lost" not in caplog.text
    finally:
        await loop.stop()
        await asyncio.wait_for(running, 2)


async def test_unexpected_tick_error_still_exits_scheduler(monkeypatch):
    class BrokenState(FakeStreamService):
        async def state(self, _task_id: str) -> dict:
            raise ValueError("invalid scheduling state")

    service, executor = BrokenState(), FakeExecutor()
    loop = SchedulerLoop(cast(ActionExecutor, executor), cast(BlackboardClient, service), Params())

    async def idle_sweeper() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(loop._sweeper, "run", idle_sweeper)
    with pytest.raises(ValueError, match="invalid scheduling state"):
        await loop.run()
    assert executor.shutdown_reasons == ["runtime_restart"]
