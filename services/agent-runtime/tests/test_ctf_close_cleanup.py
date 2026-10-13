"""Failed close retains local checkpoints and never destroys an incomplete archive."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_ctf_supervisor import supervisor


@pytest.mark.parametrize("blocker", ["unresolved", "checkpoint", "close"])
async def test_cleanup_retains_local_checkpoint_and_blocks_repeated_ticks(blocker, caplog):
    row = {"id": "ctf-1", "mode": "ctf", "status": "failed", "run_number": 2}
    instance, service, manager, coordinator = supervisor([row])
    control = {
        "unresolved": {"unresolved_checkpoints": ["turn"]},
        "checkpoint": {"checkpoint_failures": {"turn": {"run_number": 2, "purpose": "execution"}}},
        "close": {"close_failure": ["Local checkpoint was not confirmed"]},
    }[blocker]
    stale = {"task": {**row, "ctf_control": {}}}
    latest = {"task": {**row, "ctf_control": control}}
    service.state = AsyncMock(return_value=latest)
    manager.destroy = AsyncMock()
    manager.archive_task = AsyncMock()
    service.archive_data = AsyncMock()
    service.record_archive = AsyncMock()
    service.record_cleanup = AsyncMock()
    coordinator.pending_checkpoints = {"ctf-1": {"turn": object()}}
    snapshot = coordinator.pending_checkpoints["ctf-1"]["turn"]

    async def parked():
        await asyncio.Event().wait()

    future = asyncio.create_task(parked())
    instance.ctf_tasks["ctf-1"] = future
    try:
        # The state supplied by the scheduler is stale: check the database before detach.
        await instance._cleanup_task("ctf-1", stale)
        assert "ctf-1" in instance.archive_blocked
        assert not future.done()
        assert coordinator.pending_checkpoints["ctf-1"]["turn"] is snapshot
        for _ in range(3):
            await instance.tick()
        coordinator.detach.assert_not_awaited()
        manager.archive_task.assert_not_awaited()
        manager.destroy.assert_not_awaited()
        service.archive_data.assert_not_awaited()
        service.record_archive.assert_not_awaited()
        service.record_cleanup.assert_not_awaited()
        coordinator.service.runtime.assert_awaited_once_with(
            "ctf-1",
            "cleanup_status",
            phase="failed",
            error="Unconfirmed CTF checkpoint or close failure blocks archive cleanup",
        )
        assert caplog.text.count("checkpoints and environment retained") == 1
    finally:
        future.cancel()
        await asyncio.gather(future, return_exceptions=True)


async def test_previous_run_or_review_fault_does_not_block_current_archive():
    row = {"id": "ctf-1", "mode": "ctf", "status": "finished", "run_number": 2}
    instance, service, manager, coordinator = supervisor([row])
    task = {
        **row,
        "workspace_uri": "workspace/ctf-1/run-2.tar.zst",
        "ctf_control": {
            "checkpoint_failures": {
                "old": {"run_number": 1, "purpose": "execution"},
                "review": {"run_number": 2, "purpose": "review"},
            }
        },
    }
    service.state = AsyncMock(return_value={"task": task})
    manager.destroy = AsyncMock()
    service.record_cleanup = AsyncMock()
    await instance._cleanup_task("ctf-1", {"task": task})
    coordinator.detach.assert_awaited_once_with("ctf-1")
    manager.destroy.assert_awaited_once_with("ctf-1")
    service.record_cleanup.assert_awaited_once_with("ctf-1")
    assert "ctf-1" not in instance.archive_blocked
