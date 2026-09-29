"""Heartbeat and conclude deadlines use an injected clock and runner cancellation."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from bbx_contracts.models import Params
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.scheduler import sweeper as sweeper_module
from bbx_runtime.scheduler.executor import ActionExecutor
from bbx_runtime.scheduler.sweeper import Sweeper


class FakeService:
    def __init__(self, agents: dict) -> None:
        self.agents = agents
        self.calls = 0
        self.finished: list[tuple[str, str]] = []

    async def state(self, _task_id: str) -> dict:
        self.calls += 1
        return {"agents": self.agents}

    async def finish_agent(self, _task_id: str, aid: str, _receipt: dict, reason: str) -> list:
        self.finished.append((aid, reason))
        return []


class FakeExecutor:
    task_id = "task"

    def __init__(self) -> None:
        self.cancelled: list[tuple[str, str]] = []

    async def cancel(self, aid: str, reason: str) -> bool:
        self.cancelled.append((aid, reason))
        return True


async def test_sweeper_distinguishes_heartbeat_and_grace_deadlines():
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    agents = {
        "agent-1": {
            "status": "running",
            "last_heartbeat_at": (now - timedelta(minutes=31)).isoformat(),
        },
        "agent-2": {
            "status": "concluding",
            "conclude_requested_at": (now - timedelta(minutes=6)).isoformat(),
            "last_heartbeat_at": now.isoformat(),
        },
        "agent-3": {"status": "running", "last_heartbeat_at": now.isoformat()},
        "agent-4": {
            "status": "finished",
            "last_heartbeat_at": (now - timedelta(hours=1)).isoformat(),
        },
    }
    service, executor = FakeService(agents), FakeExecutor()
    sweeper = Sweeper(
        cast(ActionExecutor, executor), cast(BlackboardClient, service), Params(), now=lambda: now
    )
    await sweeper.check_once()
    assert executor.cancelled == [
        ("agent-1", "heartbeat"),
        ("agent-2", "grace_timeout"),
    ]


async def test_sweeper_runs_periodically_until_stopped(monkeypatch):
    monkeypatch.setattr(sweeper_module, "SWEEP_SECONDS", 0.02)
    service, executor = FakeService({}), FakeExecutor()
    sweeper = Sweeper(cast(ActionExecutor, executor), cast(BlackboardClient, service), Params())
    running = asyncio.create_task(sweeper.run())
    for _ in range(50):
        if service.calls >= 2:
            break
        await asyncio.sleep(0.01)
    sweeper.stop()
    await asyncio.wait_for(running, 2)
    assert service.calls >= 2


async def test_sweeper_finishes_expired_agent_missing_from_local_tasks():
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    service = FakeService(
        {
            "agent-7": {
                "status": "running",
                "last_heartbeat_at": (now - timedelta(minutes=31)).isoformat(),
            }
        }
    )

    class MissingExecutor(FakeExecutor):
        async def cancel(self, aid: str, reason: str) -> bool:
            self.cancelled.append((aid, reason))
            return False

    executor = MissingExecutor()
    sweeper = Sweeper(
        cast(ActionExecutor, executor), cast(BlackboardClient, service), Params(), now=lambda: now
    )
    await sweeper.check_once()
    assert executor.cancelled == [("agent-7", "heartbeat")]
    assert service.finished == [("agent-7", "heartbeat")]


async def test_sweeper_rechecks_state_after_lost_finish_reply(monkeypatch, caplog):
    monkeypatch.setattr(sweeper_module, "SWEEP_SECONDS", 0.02)
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    agent = {
        "status": "running",
        "last_heartbeat_at": (now - timedelta(minutes=31)).isoformat(),
    }

    class LostReplyService(FakeService):
        def __init__(self) -> None:
            super().__init__({"agent-1": agent})
            self.finish_calls = 0

        async def finish_agent(self, _task_id: str, aid: str, _receipt: dict, reason: str) -> list:
            self.finish_calls += 1
            self.agents["agent-1"]["status"] = "finished"
            raise httpx.RemoteProtocolError("finish committed, reply lost")

    class MissingExecutor(FakeExecutor):
        async def cancel(self, aid: str, reason: str) -> bool:
            self.cancelled.append((aid, reason))
            return False

    service, executor = LostReplyService(), MissingExecutor()
    sweeper = Sweeper(
        cast(ActionExecutor, executor), cast(BlackboardClient, service), Params(), now=lambda: now
    )
    running = asyncio.create_task(sweeper.run())
    try:
        for _ in range(50):
            if service.calls >= 2:
                break
            await asyncio.sleep(0.01)
        assert service.calls >= 2
        assert service.finish_calls == 1
        assert executor.cancelled == [("agent-1", "heartbeat")]
        assert not running.done()
        assert "phase=sweep error=RemoteProtocolError" in caplog.text
        assert "finish committed, reply lost" not in caplog.text
    finally:
        sweeper.stop()
        await asyncio.wait_for(running, 2)


async def test_sweeper_read_disconnect_retries_on_next_period(monkeypatch):
    monkeypatch.setattr(sweeper_module, "SWEEP_SECONDS", 0.02)

    class FlakyRead(FakeService):
        async def state(self, _task_id: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise httpx.RemoteProtocolError("lost state reply")
            return {"agents": self.agents}

    service = FlakyRead({})
    sweeper = Sweeper(
        cast(ActionExecutor, FakeExecutor()), cast(BlackboardClient, service), Params()
    )
    running = asyncio.create_task(sweeper.run())
    try:
        for _ in range(50):
            if service.calls >= 2:
                break
            await asyncio.sleep(0.01)
        assert service.calls >= 2 and not running.done()
    finally:
        sweeper.stop()
        await asyncio.wait_for(running, 2)


async def test_sweeper_unexpected_state_error_still_propagates():
    class BrokenState(FakeService):
        async def state(self, _task_id: str) -> dict:
            raise ValueError("invalid sweeper state")

    sweeper = Sweeper(
        cast(ActionExecutor, FakeExecutor()),
        cast(BlackboardClient, BrokenState({})),
        Params(),
    )
    with pytest.raises(ValueError, match="invalid sweeper state"):
        await sweeper.run()
