"""Ordinary control 5xx defer work; permanent protocol and business errors still fail."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from bbx_contracts.models import Params
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.scheduler import loop as loop_module
from bbx_runtime.scheduler import sweeper as sweep_module
from bbx_runtime.scheduler.loop import SchedulerLoop
from bbx_runtime.scheduler.sweeper import Sweeper
from test_scheduler_loop import FakeExecutor
from test_scheduler_sweeper import FakeExecutor as SweepExecutor
from test_scheduler_sweeper import FakeService


async def test_503_read_keeps_agents_and_rechecks_state_on_next_tick(monkeypatch, caplog):
    service = SimpleNamespace(
        state=AsyncMock(
            side_effect=[
                RemoteError(503, "synthetic private response"),
                {"task": {"status": "running"}},
            ]
        )
    )
    executor = FakeExecutor()
    value = SchedulerLoop(cast(Any, executor), cast(Any, service), Params())
    monkeypatch.setattr(loop_module, "decide", lambda *_: [])
    await value.tick()
    assert not executor.shutdown_reasons and executor.ticks == 0
    await value.tick()
    assert service.state.await_count == 2 and executor.ticks == 1
    assert "private" not in caplog.text


async def test_committed_action_503_is_not_replayed_after_authoritative_redecision(monkeypatch):
    service = SimpleNamespace(committed=False)

    async def state(_):
        return {"task": {"status": "running"}, "committed": service.committed}

    service.state = state
    actions = []

    class Executor(FakeExecutor):
        async def execute(self, current):
            actions.append(current)
            if current:
                service.committed = True
                raise RemoteError(503, "synthetic lost write response")

    executor = Executor()
    value = SchedulerLoop(cast(Any, executor), cast(Any, service), Params())
    monkeypatch.setattr(
        loop_module, "decide", lambda state, *_: [] if state["committed"] else ["apply"]
    )
    await value.tick()
    await value.tick()
    assert actions == [["apply"], []]
    assert not executor.shutdown_reasons


@pytest.mark.parametrize("stage", ["read", "apply"])
@pytest.mark.parametrize(
    "error",
    [
        RemoteError(422, "synthetic invalid"),
        httpx.LocalProtocolError("synthetic local"),
        httpx.UnsupportedProtocol("synthetic invalid URL"),
    ],
)
async def test_permanent_control_faults_propagate_from_tick(stage, error, monkeypatch):
    service = SimpleNamespace(state=AsyncMock(return_value={"task": {"status": "running"}}))
    executor = FakeExecutor()
    if stage == "read":
        service.state.side_effect = error
    else:
        executor.execute = cast(Any, AsyncMock(side_effect=error))
    value = SchedulerLoop(cast(Any, executor), cast(Any, service), Params())
    monkeypatch.setattr(loop_module, "decide", lambda *_: [])
    with pytest.raises(type(error)) as raised:
        await value.tick()
    assert raised.value is error


async def test_sweeper_503_after_committed_finish_rechecks_without_second_write(monkeypatch):
    monkeypatch.setattr(sweep_module, "SWEEP_SECONDS", 0.001)
    now = datetime.now(UTC)

    class Service(FakeService):
        async def state(self, tid):
            result = await super().state(tid)
            if self.calls >= 2:
                value.stop()
            return result

        async def finish_agent(self, tid, aid, receipt, reason):
            self.agents[aid]["status"] = "finished"
            await super().finish_agent(tid, aid, receipt, reason)
            raise RemoteError(503, "synthetic committed write")

    service = Service(
        {
            "agent-1": {
                "status": "running",
                "last_heartbeat_at": (now - timedelta(hours=1)).isoformat(),
            }
        }
    )
    executor = SweepExecutor()
    executor.cancel = cast(Any, AsyncMock(return_value=False))
    value = Sweeper(cast(Any, executor), cast(Any, service), Params(), now=lambda: now)
    await asyncio.wait_for(value.run(), 1)
    assert service.calls == 2
    assert service.finished == [("agent-1", "heartbeat")]


@pytest.mark.parametrize(
    "error", [RemoteError(403, "synthetic forbidden"), httpx.LocalProtocolError("synthetic local")]
)
async def test_sweeper_permanent_control_error_is_not_swallowed(error):
    service = SimpleNamespace(state=AsyncMock(side_effect=error))
    value = Sweeper(cast(Any, SweepExecutor()), cast(Any, service), Params())
    with pytest.raises(type(error)) as raised:
        await value.run()
    assert raised.value is error
