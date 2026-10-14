"""Existing recovery callers prefer the saved final receipt to generic interruption."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from bbx_contracts.models import Params
from bbx_runtime.scheduler.sweeper import Sweeper
from test_supervisor import Manager, Service, supervisor, task


class ReceiptService(Service):
    async def finish_agent(self, tid, aid, receipt, reason, **fence):
        assert fence.get("expected_derive_round") == 2
        return await super().finish_agent(tid, aid, receipt, reason)

    async def conclude(self, tid, aid, reason, **fence):
        assert fence.get("expected_derive_round") == 2
        return await super().conclude(tid, aid, reason)


def expired_agent():
    return {
        "status": "running",
        "task_type": "derive",
        "derive_round": 2,
        "run_id": "synthetic-agent-run",
        "last_heartbeat_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }


@pytest.mark.parametrize("matching", [True, False])
async def test_sweeper_prefers_matching_receipt_after_local_writer_has_gone(matching):
    agent = expired_agent()
    snapshot = {"run_number": 2, "run_id": "synthetic-task-run"}
    reconcile = AsyncMock(return_value=matching)
    service = SimpleNamespace(
        state=AsyncMock(return_value={"task": snapshot, "agents": {"agent-1": agent}}),
        finish_agent=AsyncMock(),
    )
    executor = SimpleNamespace(
        task_id="task",
        cancel=AsyncMock(return_value=False),
        runner=SimpleNamespace(reconcile_final_receipt=reconcile),
    )
    value = Sweeper(cast(Any, executor), cast(Any, service), Params())
    await value.check_once()
    reconcile.assert_awaited_once_with("task", "agent-1", agent, snapshot)
    assert service.finish_agent.await_count == (0 if matching else 1)


async def test_active_runner_keeps_ownership_of_its_shielded_finalization():
    reconcile = AsyncMock(return_value=True)
    service = SimpleNamespace(
        state=AsyncMock(return_value={"task": {}, "agents": {"agent-1": expired_agent()}}),
        finish_agent=AsyncMock(),
    )
    executor = SimpleNamespace(
        task_id="task",
        cancel=AsyncMock(return_value=True),
        runner=SimpleNamespace(reconcile_final_receipt=reconcile),
    )
    await Sweeper(cast(Any, executor), cast(Any, service), Params()).check_once()
    reconcile.assert_not_awaited()
    service.finish_agent.assert_not_awaited()


@pytest.mark.parametrize("matching", [True, False])
async def test_restart_recovery_preserves_receipt_without_waiting_for_all_pending_tasks(
    matching, monkeypatch
):
    service = ReceiptService([task("first", "running"), task("queued", "provisioning", 1)])
    service.states["first"]["agents"] = {"agent-1": expired_agent()}
    manager = Manager()
    manager.handles["first"] = SimpleNamespace(task_id="first")
    instance = supervisor(service, manager)
    reconcile = AsyncMock(return_value=matching)
    monkeypatch.setattr(instance.runner, "reconcile_final_receipt", reconcile, raising=False)
    monkeypatch.setattr(instance, "_start", AsyncMock())
    await instance.recover()
    reconcile.assert_awaited_once()
    assert len([call for call in service.calls if call[0] == "finish"]) == (0 if matching else 1)
    assert ("find", "queued") in manager.calls
    assert service.states["queued"]["task"]["status"] == "provisioning"


@pytest.mark.parametrize("matching", [True, False])
async def test_terminal_drain_waits_for_original_receipt_instead_of_generic_finish(
    matching, monkeypatch
):
    service = ReceiptService([task("done", "finished")])
    service.states["done"]["agents"] = {"agent-1": expired_agent()}
    instance = supervisor(service, Manager())
    calls = []

    async def reconcile(tid, aid, agent, snapshot):
        calls.append((tid, aid, snapshot["status"]))
        if matching and len(calls) == 2:
            service.states[tid]["agents"][aid]["status"] = "finished"
        return matching

    monkeypatch.setattr(instance.runner, "reconcile_final_receipt", reconcile, raising=False)
    await instance._drain("done", await service.state("done"))
    assert len(calls) == (2 if matching else 1)
    finish_calls = [call for call in service.calls if call[0] == "finish"]
    assert len(finish_calls) == (0 if matching else 1)
