"""Supervisor keeps task capacity and archive state durable across restarts."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from bbx_contracts.profile import load_profile
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.execenv import ArchiveResult, ExecEnvManager
from bbx_runtime.scheduler.loop import SchedulerLoop
from bbx_runtime.scheduler.supervisor import TaskSupervisor
from bbx_runtime.settings import Settings

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


def task(tid: str, status: str, minutes: int = 0) -> dict:
    return {
        "id": tid,
        "status": status,
        "created_at": (NOW + timedelta(minutes=minutes)).isoformat(),
        "agent_profile": "default",
        "agent_profile_version": 1,
    }


class Service:
    def __init__(self, tasks: list[dict]) -> None:
        self.tasks = tasks
        self.states = {
            row["id"]: {
                "task": {**row, "params": {}, "workspace_uri": None},
                "agents": {},
                "intents": {},
            }
            for row in tasks
        }
        self.calls: list[tuple] = []
        self.record_error = False

    async def list_tasks(self) -> list[dict]:
        return deepcopy(self.tasks)

    async def state(self, tid: str) -> dict:
        return deepcopy(self.states[tid])

    async def get_profile(self, name: str, version: int) -> dict:
        self.calls.append(("profile", name, version))
        profile, _ = load_profile(ROOT / "profiles/default")
        return {"profile": profile.model_dump(mode="json")}

    async def transition(self, tid: str, status: str, reason: str | None = None) -> list:
        self.calls.append(("transition", tid, status, reason))
        self.states[tid]["task"]["status"] = status
        next(row for row in self.tasks if row["id"] == tid)["status"] = status
        return []

    async def finish_agent(self, tid: str, aid: str, receipt: dict, reason: str) -> list:
        self.calls.append(("finish", tid, aid, reason))
        self.states[tid]["agents"][aid]["status"] = "finished"
        self.states[tid]["agents"][aid]["end_reason"] = reason
        return []

    async def conclude(self, tid: str, aid: str, reason: str) -> list:
        self.calls.append(("conclude", tid, aid, reason))
        self.states[tid]["agents"][aid]["status"] = "concluding"
        self.states[tid]["agents"][aid]["conclude_requested_at"] = NOW.isoformat()
        return []

    async def record_archive(self, tid: str, uri: str, size: int, fallback: str) -> list:
        self.calls.append(("record", tid, uri, size, fallback))
        if self.record_error:
            raise RuntimeError("record failed")
        self.states[tid]["task"]["workspace_uri"] = uri
        return []


class Manager:
    def __init__(self) -> None:
        self.handles: dict[str, SimpleNamespace] = {}
        self.calls: list[tuple] = []
        self.archive_gate: asyncio.Event | None = None

    async def find(self, tid: str) -> SimpleNamespace | None:
        self.calls.append(("find", tid))
        return self.handles.get(tid)

    async def wait_healthy(self, handle: SimpleNamespace) -> None:
        self.calls.append(("healthy", handle.task_id))

    async def provision(self, tid: str, _profile) -> SimpleNamespace:
        self.calls.append(("provision", tid))
        handle = SimpleNamespace(task_id=tid)
        self.handles[tid] = handle
        return handle

    async def archive_to_store(self, handle: SimpleNamespace) -> ArchiveResult:
        self.calls.append(("archive", handle.task_id))
        if self.archive_gate is not None:
            await self.archive_gate.wait()
        return ArchiveResult(f"workspace/{handle.task_id}.tar.zst", 123, "none")

    async def destroy(self, tid: str) -> None:
        self.calls.append(("destroy", tid))
        self.handles.pop(tid, None)


def supervisor(service: Service, manager: Manager, *, limit: int = 1) -> TaskSupervisor:
    return TaskSupervisor(
        Settings.model_construct(max_running_tasks=limit),
        cast(BlackboardClient, service),
        cast(ExecEnvManager, manager),
        Mock(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_fifo_queue_respects_max_running_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Service([task("new", "provisioning", 2), task("old", "provisioning", 0)])
    manager = Manager()
    owner = supervisor(service, manager)
    started = AsyncMock()
    monkeypatch.setattr(owner, "_start", started)
    await owner.tick()
    assert [call for call in manager.calls if call[0] == "provision"] == [("provision", "old")]
    assert service.states["old"]["task"]["status"] == "running"
    assert service.states["new"]["task"]["status"] == "provisioning"
    started.assert_awaited_once()
    await owner.tick()
    assert [call for call in manager.calls if call[0] == "provision"] == [("provision", "old")]


@pytest.mark.asyncio
async def test_recovery_keeps_unprovisioned_queue_and_fails_missing_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("queued", "provisioning"), task("running", "running")])
    service.states["running"]["agents"]["agent-1"] = {"status": "running"}
    service.states["running"]["intents"]["I1"] = {"attempts": 2, "holder": "agent-1"}
    manager = Manager()
    owner = supervisor(service, manager)
    monkeypatch.setattr(owner, "_start", AsyncMock())
    await owner.recover()
    assert service.states["queued"]["task"]["status"] == "provisioning"
    assert service.states["running"]["task"]["status"] == "failed"
    assert ("finish", "running", "agent-1", "runtime_restart") in service.calls
    assert service.states["running"]["intents"]["I1"]["attempts"] == 2


@pytest.mark.asyncio
async def test_recovery_rejoins_running_and_existing_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("queued", "provisioning"), task("running", "running")])
    manager = Manager()
    manager.handles = {
        "queued": SimpleNamespace(task_id="queued"),
        "running": SimpleNamespace(task_id="running"),
    }
    owner = supervisor(service, manager)
    started = AsyncMock()
    monkeypatch.setattr(owner, "_start", started)
    await owner.recover()
    assert ("healthy", "queued") in manager.calls
    assert ("healthy", "running") in manager.calls
    started.assert_awaited_once()
    assert started.call_args.args[0]["task"]["id"] == "running"
    assert service.states["queued"]["task"]["status"] == "provisioning"


@pytest.mark.asyncio
async def test_archive_record_precedes_destroy_and_failure_retains_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("done", "finished")])
    manager = Manager()
    manager.handles["done"] = SimpleNamespace(task_id="done")
    owner = supervisor(service, manager)
    original_destroy = manager.destroy

    async def destroy_only_after_record(tid: str) -> None:
        assert service.states[tid]["task"]["workspace_uri"] == f"workspace/{tid}.tar.zst"
        await original_destroy(tid)

    monkeypatch.setattr(manager, "destroy", destroy_only_after_record)
    service.record_error = True
    with pytest.raises(RuntimeError, match="record failed"):
        await owner._cleanup("done")
    assert "done" in manager.handles
    assert ("destroy", "done") not in manager.calls
    service.record_error = False
    await owner._cleanup("done")
    assert service.states["done"]["task"]["workspace_uri"] == "workspace/done.tar.zst"
    assert [call[0] for call in manager.calls if call[0] in {"archive", "destroy"}] == [
        "archive",
        "archive",
        "destroy",
    ]
    assert [call[0] for call in service.calls if call[0] == "record"] == ["record", "record"]


@pytest.mark.asyncio
async def test_cleanup_occupies_slot_until_archive_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("done", "finished"), task("waiting", "provisioning", 1)])
    manager = Manager()
    manager.handles["done"] = SimpleNamespace(task_id="done")
    manager.archive_gate = asyncio.Event()
    owner = supervisor(service, manager)
    monkeypatch.setattr(owner, "_start", AsyncMock())
    await owner.tick()
    assert "done" in owner.cleanups
    assert ("provision", "waiting") not in manager.calls
    manager.archive_gate.set()
    await asyncio.wait_for(owner.cleanups["done"], 2)
    await owner.tick()
    assert ("provision", "waiting") in manager.calls


@pytest.mark.asyncio
async def test_stop_does_not_destroy_running_task() -> None:
    service = Service([task("active", "running")])
    manager = Manager()
    manager.handles["active"] = SimpleNamespace(task_id="active")
    owner = supervisor(service, manager)
    loop = SimpleNamespace(stop=AsyncMock(), request_stop=Mock())
    owner.loops["active"] = cast(SchedulerLoop, loop)
    owner.loop_tasks["active"] = asyncio.create_task(asyncio.sleep(0))
    await owner.stop()
    loop.request_stop.assert_called_once_with("runtime_restart")
    loop.stop.assert_awaited_once_with(reason="runtime_restart")
    assert "active" in manager.handles
    assert not any(call[0] in {"archive", "destroy"} for call in manager.calls)


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_provision(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Service([task("first", "provisioning"), task("second", "provisioning", 1)])
    manager = Manager()
    owner = supervisor(service, manager)
    monkeypatch.setattr(owner, "_start", AsyncMock())
    entered = asyncio.Event()
    release = asyncio.Event()
    original = manager.provision

    async def slow_provision(tid: str, profile):
        entered.set()
        await release.wait()
        return await original(tid, profile)

    monkeypatch.setattr(manager, "provision", slow_provision)
    ticking = asyncio.create_task(owner.tick())
    await asyncio.wait_for(entered.wait(), 2)
    stopping = asyncio.create_task(owner.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(ticking, stopping), 2)
    assert ("provision", "first") in manager.calls
    assert ("provision", "second") not in manager.calls
    assert "first" in manager.handles
    assert ("destroy", "first") not in manager.calls


@pytest.mark.asyncio
async def test_terminal_during_provision_keeps_slot_until_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Service([task("first", "provisioning"), task("second", "provisioning", 1)])
    manager = Manager()
    owner = supervisor(service, manager)
    monkeypatch.setattr(owner, "_start", AsyncMock())
    original = manager.provision

    async def provision_then_stop(tid: str, profile):
        handle = await original(tid, profile)
        await service.transition(tid, "stopped")
        return handle

    monkeypatch.setattr(manager, "provision", provision_then_stop)
    await owner.tick()
    assert service.states["first"]["task"]["status"] == "stopped"
    assert service.states["second"]["task"]["status"] == "provisioning"
    assert ("provision", "second") not in manager.calls
    assert "first" in manager.handles
